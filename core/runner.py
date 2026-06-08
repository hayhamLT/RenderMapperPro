from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .models import JobConfig

LogCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]


def build_blender_command(
    blender_path: str, scene_path: str, worker_path: str, config_path: str
) -> list[str]:
    """Pure builder for the headless Blender invocation. A .blend is opened
    directly; other formats are imported by the worker, so only .blend is passed
    as the positional scene arg. Extracted for unit-testing."""
    command = [blender_path, "-b"]
    if str(scene_path).lower().endswith(".blend"):
        command.append(str(scene_path))
    command.extend(["--python", str(worker_path), "--", str(config_path)])
    return command

def submit_deadline_job(
    blender_executable: str,
    worker_script_path: str,
    job: JobConfig,
    on_log: LogCallback | None = None,
    c4dpy_executable: str = "",
    c4d_worker_script: str = "",
) -> int:
    import uuid

    is_c4d = is_c4d_scene(job.scene_path)
    scene_path = Path(job.scene_path).expanduser().resolve()
    blender_path = os.path.expanduser(blender_executable)
    c4dpy_path = os.path.expanduser(c4dpy_executable) if c4dpy_executable else ""
    worker_path = Path(c4d_worker_script if is_c4d else worker_script_path).expanduser().resolve()

    if not scene_path.exists():
        raise FileNotFoundError(f"Scene file not found: {scene_path}")
    if not worker_path.exists():
        raise FileNotFoundError(f"Worker script not found: {worker_path}")
    if is_c4d and not c4dpy_path:
        raise RuntimeError("c4dpy path is required to submit a Cinema 4D job to Deadline.")

    submit_scene = getattr(job, 'submit_scene', True)
    repo_path = getattr(job, 'deadline_repo_path', '').strip()

    if on_log:
        on_log(f"[deadline] submit_scene={submit_scene}")

    # ── Staging directory ────────────────────────────────────────────────────
    # Blender resolves ALL relative paths (both -b and --python) relative to
    # its own app Resources dir, NOT the process CWD.  The only safe approach
    # is to pass fully-qualified absolute paths for every file.
    #
    # The Deadline Repository is the one share guaranteed to be mounted on
    # every worker (Deadline requires it to function), so we stage all job
    # files there and reference them with absolute paths in the Arguments.
    staging_id = uuid.uuid4().hex[:16]
    if repo_path and Path(repo_path).exists():
        staging_dir = Path(repo_path) / "custom" / "blender_jobs" / staging_id
    else:
        # Fallback: local temp dir (works only on single-machine setups)
        staging_dir = Path(tempfile.gettempdir()) / "blender_jobs" / staging_id

    staging_dir.mkdir(parents=True, exist_ok=True)
    if on_log:
        on_log(f"[deadline] Staging job files to: {staging_dir}")

    def _safe_copy(src: str | Path, dst: str | Path) -> None:
        """Copy a file, falling back to data-only copy on network volumes.

        shutil.copy2 tries to preserve timestamps/xattrs; this fails with
        EINVAL (errno 22) on SMB/NFS mounts such as the Deadline Repository.
        shutil.copy copies only the file data and skips metadata, which is
        all we need for staging.
        """
        try:
            shutil.copy2(str(src), str(dst))
        except OSError:
            shutil.copy(str(src), str(dst))

    # Stage blender_worker.py (small – always copied)
    staged_worker = staging_dir / worker_path.name
    _safe_copy(worker_path, staged_worker)

    # Build the config dict
    cfg_dict = job.to_json_dict()

    # Decide what scene path the worker should use
    if submit_scene:
        # Copy the .blend into the staging dir so workers don't need the
        # source drive mounted.
        if on_log:
            on_log(f"[deadline] Copying scene to staging dir ({scene_path.stat().st_size // 1024 // 1024} MB)…")
        staged_scene = staging_dir / scene_path.name
        _safe_copy(scene_path, staged_scene)
        scene_arg = str(staged_scene)
        cfg_dict["scene_path"] = str(staged_scene)

        # Video files: copy if they exist locally, otherwise leave absolute
        # path (worker must be able to reach the share itself).
        def _stage_video(vp_str: str) -> str:
            vp = Path(vp_str).expanduser().resolve()
            if not vp.exists():
                return vp_str  # keep original; worker will get a clear error
            staged = staging_dir / vp.name
            if not staged.exists():
                if on_log:
                    on_log(f"[deadline] Copying video {vp.name} to staging dir ({vp.stat().st_size // 1024 // 1024} MB)…")
                _safe_copy(vp, staged)
            return str(staged)

        if cfg_dict.get("video_path"):
            cfg_dict["video_path"] = _stage_video(job.video_path)
        for asn in cfg_dict.get("material_assignments", []):
            if asn.get("video_path"):
                asn["video_path"] = _stage_video(asn["video_path"])
    else:
        scene_arg = str(scene_path)  # absolute path; workers must share the drive

    # The Cinema 4D worker shells out to ffmpeg to extract video frames; stage
    # the bundled binary so farm nodes don't depend on a local install.
    if is_c4d:
        ff = str(cfg_dict.get("ffmpeg_path", "") or "")
        ffp = Path(ff).expanduser() if ff else None
        if ffp and ffp.exists():
            staged_ff = staging_dir / ffp.name
            _safe_copy(ffp, staged_ff)
            try:
                os.chmod(staged_ff, 0o755)
            except OSError:
                pass
            cfg_dict["ffmpeg_path"] = str(staged_ff)

    # Write the config JSON into the staging dir with a fixed name
    staged_config = staging_dir / "blender_job_config.json"
    staged_config.write_text(json.dumps(cfg_dict))

    # ── Job info file ────────────────────────────────────────────────────────
    job_info_path = staging_dir / "job_info.job"
    plugin_info_path = staging_dir / "plugin_info.job"

    with open(job_info_path, "w") as f:
        name_template = getattr(job, 'deadline_job_name_template', "")
        if name_template:
            try:
                name = name_template.format(
                    scene_name=scene_path.name,
                    video_name=Path(job.video_path).name if job.video_path else "",
                )
            except Exception:
                name = f"BlenderRender Job - {scene_path.name}"
        else:
            name = f"BlenderRender Job - {scene_path.name}"

        f.write(f"Name={name}\n")
        f.write("Plugin=CommandLine\n")
        f.write(f"Frames={job.render.frame_start}-{job.render.frame_end}\n")
        f.write(f"Priority={getattr(job, 'deadline_priority', 50)}\n")

        pool = getattr(job, 'deadline_pool', "")
        if pool:
            f.write(f"Pool={pool}\n")
        sec_pool = getattr(job, 'deadline_secondary_pool', "")
        if sec_pool:
            f.write(f"SecondaryPool={sec_pool}\n")
        group = getattr(job, 'deadline_group', "")
        if group:
            f.write(f"Group={group}\n")
        comment = getattr(job, 'deadline_comment', "")
        if comment:
            f.write(f"Comment={comment}\n")
        dept = getattr(job, 'deadline_department', "")
        if dept:
            f.write(f"Department={dept}\n")

        from .utils import ext_for_format
        ext = ext_for_format(job.render.output_format)
        is_video = ext != ""
        chunk_size = getattr(job, 'deadline_chunk_size', 1)
        if is_video:
            chunk_size = max(1, job.render.frame_end - job.render.frame_start + 1)
        if chunk_size > 1:
            f.write(f"ChunkSize={chunk_size}\n")

        if getattr(job, 'deadline_suspended', False):
            f.write("InitialStatus=Suspended\n")
        machine_limit = getattr(job, 'deadline_machine_limit', 0)
        if machine_limit > 0:
            f.write(f"MachineLimit={machine_limit}\n")
        limits = getattr(job, 'deadline_limits', "")
        if limits:
            f.write(f"Limits={limits}\n")
        whitelist = getattr(job, 'deadline_whitelist', "").strip()
        if whitelist:
            f.write(f"Whitelist={whitelist}\n")

        if job.output_path:
            out_path = Path(job.output_path)
            if out_path.suffix:
                f.write(f"OutputDirectory0={out_path.parent}\n")
                f.write(f"OutputFilename0={out_path.name}\n")
            else:
                f.write(f"OutputDirectory0={out_path}\n")
                ext = ext_for_format(job.render.output_format) or ".png"
                f.write(f"OutputFilename0=####{ext}\n")

    # ── Plugin info file ─────────────────────────────────────────────────────
    # All paths are fully-qualified absolutes — no CWD ambiguity.
    with open(plugin_info_path, "w") as f:
        if is_c4d:
            # c4dpy prompts for a license method on stdin and can't take a scene
            # via flags, so a tiny staged launcher feeds the prompt and runs our
            # worker. Deadline substitutes the per-task frame range into the
            # launcher's args, which the worker honours.
            win = c4dpy_path.lower().endswith(".exe") or "\\" in c4dpy_path
            if win:
                launcher = staging_dir / "c4d_launch.bat"
                launcher.write_text(
                    "@echo off\r\n"
                    f'echo 1| "{c4dpy_path}" "{staged_worker}" "{staged_config}" %1 %2\r\n'
                )
            else:
                launcher = staging_dir / "c4d_launch.sh"
                launcher.write_text(
                    "#!/bin/sh\n"
                    f"printf '1\\n' | \"{c4dpy_path}\" \"{staged_worker}\" \"{staged_config}\" \"$1\" \"$2\"\n"
                )
                try:
                    os.chmod(launcher, 0o755)
                except OSError:
                    pass
            f.write(f"Executable={launcher}\n")
            f.write("Arguments=<STARTFRAME> <ENDFRAME>\n")
        else:
            f.write(f"Executable={blender_path}\n")
            # Pass the scene via -b only when we have a guaranteed absolute path.
            # The worker script also opens the scene, but Blender's -b is needed
            # to pre-load scene data before the Python script runs.
            f.write(
                f'Arguments=-b "{scene_arg}"'
                f' --python "{staged_worker}"'
                f' -- "{staged_config}"\n'
            )

    # ── Submit ───────────────────────────────────────────────────────────────
    from .utils import find_deadlinecommand
    deadline_cmd = getattr(job, 'deadline_command_path', "").strip()
    if not deadline_cmd:
        deadline_cmd = find_deadlinecommand() or "deadlinecommand"

    if repo_path:
        cmd = [deadline_cmd, "RunCommandForRepository", "Direct", repo_path,
               "SubmitJob", str(job_info_path), str(plugin_info_path)]
    else:
        cmd = [deadline_cmd, str(job_info_path), str(plugin_info_path)]
    # No auxiliary files — everything is already at absolute paths in staging_dir

    if on_log:
        on_log(f"[deadline] Submitting job: frames {job.render.frame_start}-{job.render.frame_end}")
        on_log("[deadline] Command: " + " ".join(cmd))

    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        if process.stdout is not None:
            for line in process.stdout:
                if on_log:
                    on_log(line.rstrip())
        return process.wait()
    finally:
        pass


C4D_LICENSE_INPUT = "1\n"   # selects "Maxon App" at c4dpy's license-method prompt


def is_c4d_scene(scene_path: str) -> bool:
    return str(scene_path).lower().endswith(".c4d")


def run_c4d_job(
    c4dpy_executable: str,
    worker_script_path: str,
    job: JobConfig,
    on_log: LogCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> int:
    """Render a .c4d via c4dpy (Cinema 4D headless Python) + the C4D worker.

    c4dpy resolves the script path from its own install dir, so an absolute
    worker path is required. The license method is fed on stdin. c4dpy can throw
    during interpreter teardown, so success is judged by the produced PNGs rather
    than the exit code."""
    scene_path = Path(job.scene_path).expanduser().resolve()
    c4dpy = os.path.expanduser(c4dpy_executable)
    worker_path = Path(worker_script_path).expanduser().resolve()
    if not scene_path.exists():
        raise FileNotFoundError(f"Scene file not found: {scene_path}")
    if not worker_path.exists():
        raise FileNotFoundError(f"C4D worker not found: {worker_path}")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        config_path = Path(tf.name)
        json.dump(job.to_json_dict(), tf)

    command = [c4dpy, str(worker_path), str(config_path)]
    if on_log:
        on_log("[app] Executing C4D: " + " ".join(command))

    out_path = Path(job.output_path).expanduser()
    before = set(out_path.glob("*.png")) if out_path.is_dir() else set()
    start_ts = time.time()

    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    try:
        if process.stdin:
            process.stdin.write(C4D_LICENSE_INPUT)
            process.stdin.flush()
            process.stdin.close()
        if process.stdout is not None:
            for line in process.stdout:
                if should_cancel and should_cancel():
                    process.terminate()
                    break
                if on_log:
                    on_log(line.rstrip())
        rc = process.wait()
    finally:
        if process.poll() is None:
            process.kill()

    # c4dpy often crashes on exit after a successful render — treat the job as
    # successful if it produced output: new frames in a sequence folder, or a
    # freshly written movie file at the output path.
    produced_seq = (set(out_path.glob("*.png")) - before) if out_path.is_dir() else set()
    produced_movie = (
        out_path.is_file() and out_path.stat().st_size > 0
        and out_path.stat().st_mtime >= start_ts - 1
    )
    if produced_seq or produced_movie:
        return 0
    return rc if rc != 0 else 1


def run_blender_job(
    blender_executable: str,
    worker_script_path: str,
    job: JobConfig,
    on_log: LogCallback | None = None,
    should_cancel: CancelCheck | None = None,
    c4dpy_executable: str = "",
    c4d_worker_script: str = "",
) -> int:
    # Route Cinema 4D scenes to the C4D/Redshift backend.
    if is_c4d_scene(job.scene_path) and c4dpy_executable and c4d_worker_script:
        return run_c4d_job(c4dpy_executable, c4d_worker_script, job, on_log, should_cancel)

    scene_path = Path(job.scene_path).expanduser().resolve()
    blender_path = os.path.expanduser(blender_executable)
    worker_path = Path(worker_script_path).expanduser().resolve()

    if not scene_path.exists():
        raise FileNotFoundError(f"Scene file not found: {scene_path}")
    if not Path(worker_path).exists():
        raise FileNotFoundError(f"Worker script not found: {worker_path}")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
        config_path = Path(tf.name)
        json.dump(job.to_json_dict(), tf)

    command = build_blender_command(blender_path, str(scene_path), str(worker_path), str(config_path))

    hard_timeout = int(getattr(job.render, "timeout_seconds", 0) or 0)
    idle_timeout = int(getattr(job.render, "idle_timeout_seconds", 0) or 0)

    if on_log:
        on_log("[app] Executing: " + " ".join(command))

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        if process.stdout is not None:
            started = time.time()
            last_output = time.time()

            while True:
                if should_cancel and should_cancel():
                    if on_log:
                        on_log("[app] Cancel requested, stopping Blender process...")
                    process.terminate()
                    break

                now = time.time()
                if hard_timeout > 0 and (now - started) > hard_timeout:
                    if on_log:
                        on_log(f"[app] Hard timeout reached ({hard_timeout}s), terminating Blender process...")
                    process.terminate()
                    break

                if idle_timeout > 0 and (now - last_output) > idle_timeout:
                    if on_log:
                        on_log(f"[app] Idle timeout reached ({idle_timeout}s without output), terminating Blender process...")
                    process.terminate()
                    break

                if process.poll() is not None:
                    break

                ready, _, _ = select.select([process.stdout], [], [], 0.5)
                if not ready:
                    continue

                line = process.stdout.readline()
                if not line:
                    continue

                last_output = time.time()
                if on_log:
                    on_log(line.rstrip())

            # Drain any remaining buffered output after process exit so final
            # Blender/worker errors are not dropped.
            for line in process.stdout:
                if on_log:
                    on_log(line.rstrip())

        return process.wait()
    finally:
        if config_path.exists():
            config_path.unlink(missing_ok=True)
