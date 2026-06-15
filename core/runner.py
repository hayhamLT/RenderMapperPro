from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from .models import JobConfig, SceneBackend, scene_backend
from .utils import ext_for_format, iter_process_output, subprocess_creation_flags, terminate_process

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

def _write_job_info_common(f, job: JobConfig, name: str) -> None:
    """Write the pool/group/priority/etc. job-info fields shared by both
    backends. The caller writes Plugin, Frames and Output* lines."""
    f.write(f"Name={name}\n")
    f.write(f"Priority={getattr(job, 'deadline_priority', 50)}\n")
    for key, attr in (("Pool", "deadline_pool"), ("SecondaryPool", "deadline_secondary_pool"),
                      ("Group", "deadline_group"), ("Comment", "deadline_comment"),
                      ("Department", "deadline_department"), ("Limits", "deadline_limits")):
        val = str(getattr(job, attr, "") or "").strip()
        if val:
            f.write(f"{key}={val}\n")
    wl = str(getattr(job, "deadline_whitelist", "") or "").strip()
    if wl:
        f.write(f"Whitelist={wl}\n")
    ml = getattr(job, "deadline_machine_limit", 0)
    if ml and ml > 0:
        f.write(f"MachineLimit={ml}\n")
    if getattr(job, "deadline_suspended", False):
        f.write("InitialStatus=Suspended\n")


def write_commandline_job_info(f, job: JobConfig, scene_name: str, video_name: str = "",
                               on_log: LogCallback | None = None) -> None:
    """Write a complete CommandLine (Blender) Deadline job-info file. The single
    source of truth shared by farm submission AND the 'Export job files' action,
    so the two can never drift (they had)."""
    template = getattr(job, "deadline_job_name_template", "") or ""
    name = f"Render Mapper Pro Job - {scene_name}"
    if template:
        try:
            name = template.format(scene_name=scene_name, video_name=video_name)
        except Exception:
            pass
    _write_job_info_common(f, job, name)
    f.write("Plugin=CommandLine\n")
    f.write(f"Frames={job.render.frame_start}-{job.render.frame_end}\n")

    ext = ext_for_format(job.render.output_format)
    chunk = getattr(job, "deadline_chunk_size", 1)
    frame_count = job.render.frame_end - job.render.frame_start + 1
    if ext != "":                       # video → one indivisible chunk
        chunk = max(1, frame_count)
    elif chunk > frame_count:
        # A chunk larger than the range can't be split — wastes a farm round-trip.
        if on_log:
            on_log(f"[deadline] Chunk size {chunk} exceeds the {frame_count}-frame "
                   f"range; clamping to {frame_count}.")
        chunk = frame_count
    chunk = max(1, chunk)
    if chunk > 1:
        f.write(f"ChunkSize={chunk}\n")

    if job.output_path:
        out_path = Path(job.output_path)
        if out_path.suffix:
            f.write(f"OutputDirectory0={out_path.parent}\n")
            f.write(f"OutputFilename0={out_path.name}\n")
        else:
            f.write(f"OutputDirectory0={out_path}\n")
            f.write(f"OutputFilename0=####{ext or '.png'}\n")


def _submit_deadline_files(job, job_info_path, plugin_info_path, on_log) -> int:
    """Invoke deadlinecommand to submit the prepared job/plugin info files."""
    from .utils import find_deadlinecommand
    deadline_cmd = getattr(job, 'deadline_command_path', "").strip() or find_deadlinecommand() or "deadlinecommand"
    repo_path = getattr(job, 'deadline_repo_path', '').strip()
    if repo_path:
        cmd = [deadline_cmd, "RunCommandForRepository", "Direct", repo_path,
               "SubmitJob", str(job_info_path), str(plugin_info_path)]
    else:
        cmd = [deadline_cmd, str(job_info_path), str(plugin_info_path)]
    if on_log:
        on_log("[deadline] Command: " + " ".join(cmd))
    process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                               creationflags=subprocess_creation_flags())
    try:
        if process.stdout is not None:
            for line in process.stdout:
                if on_log:
                    on_log(line.rstrip())
        return process.wait()
    finally:
        if process.poll() is None:
            terminate_process(process)


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

    # ── Cinema 4D: bake locally, render with the licensed Commandline ────────
    # Cinema 4D jobs are baked into a self-contained .c4d here (clip → Redshift
    # image-sequence emission) using the workstation's licensed c4dpy, then
    # rendered on the farm by the licensed Cinema 4D Commandline renderer (the
    # same engine the stock Cinema4D plugin uses) — so node licensing just works
    # and no Python/c4dpy runs on the nodes.
    if is_c4d:
        if not c4dpy_path:
            raise RuntimeError("c4dpy is required on the submitting machine to bake the Cinema 4D scene.")
        prepared = staging_dir / "prepared.c4d"
        prep_cfg = job.to_json_dict()
        prep_cfg["scene_path"] = str(scene_path)
        prep_cfg["prepare_c4d_path"] = str(prepared)
        prep_cfg["sequence_dir"] = str(staging_dir / "seq")
        prep_cfg_path = staging_dir / "prepare_config.json"
        prep_cfg_path.write_text(json.dumps(prep_cfg))
        if on_log:
            on_log("[deadline] Baking Cinema 4D scene for the farm (extracting clip frames)…")
        proc = subprocess.Popen([c4dpy_path, str(worker_path), str(prep_cfg_path)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                creationflags=subprocess_creation_flags())
        if proc.stdin:
            proc.stdin.write(C4D_LICENSE_INPUT)
            proc.stdin.flush()
            proc.stdin.close()
        if proc.stdout is not None:
            for line in proc.stdout:
                if on_log:
                    on_log(line.rstrip())
        proc.wait()
        if not prepared.exists():
            raise RuntimeError("Cinema 4D scene bake failed — prepared .c4d was not produced.")
        # A bake that produced no clip frames means ffmpeg extraction failed — refuse
        # to submit a blank/unmapped render that would "succeed" on the farm. The
        # user can override this guard (force_submit) when they know better.
        seq_dir = staging_dir / "seq"
        no_frames = not (seq_dir.is_dir() and any(seq_dir.glob("*.png")))
        if job.material_assignments and no_frames:
            if getattr(job, "force_submit", False):
                if on_log:
                    on_log("[deadline] WARNING: bake produced no clip frames, but "
                           "force-submit is on — submitting anyway.")
            else:
                raise RuntimeError("Cinema 4D bake produced no clip frames (ffmpeg extraction "
                                   "likely failed) — refusing to submit a blank render. Enable "
                                   "“Force submit” in Tools to override.")

        out_p = Path(job.output_path)
        if out_p.suffix:           # a movie/file path → frames next to it
            out_dir, out_prefix = out_p.parent, out_p.stem + "_"
        else:
            out_dir, out_prefix = out_p, scene_path.stem + "_"

        def _rel(p):
            try:
                return str(Path(p).relative_to(Path(repo_path))).replace("\\", "/") if repo_path else ""
            except ValueError:
                return ""

        name = f"RenderMapperPro - {scene_path.name}"
        job_info_path = staging_dir / "job_info.job"
        plugin_info_path = staging_dir / "plugin_info.job"
        with open(job_info_path, "w") as f:
            _write_job_info_common(f, job, name)
            f.write("Plugin=RenderMapperPro\n")
            f.write(f"Frames={job.render.frame_start}-{job.render.frame_end}\n")
            chunk = getattr(job, "deadline_chunk_size", 1)
            frame_count = job.render.frame_end - job.render.frame_start + 1
            if chunk and chunk > frame_count:
                if on_log:
                    on_log(f"[deadline] Chunk size {chunk} exceeds the {frame_count}-frame "
                           f"range; clamping to {frame_count}.")
                chunk = frame_count
            if chunk and chunk > 1:
                f.write(f"ChunkSize={chunk}\n")
            f.write(f"OutputDirectory0={out_dir}\n")
            f.write(f"OutputFilename0={out_prefix}####.png\n")
        with open(plugin_info_path, "w") as f:
            f.write(f"SubmitRepoRoot={repo_path}\n")
            f.write(f"SceneFile={_rel(prepared)}\n")
            f.write(f"OutputDirectory={out_dir}\n")
            f.write(f"OutputPrefix={out_prefix}\n")
            f.write("CommandlineExecutable=\n")   # blank = node auto-detects
        return _submit_deadline_files(job, job_info_path, plugin_info_path, on_log)

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

    # (Cinema 4D is handled above and returns early; everything below is the
    # Blender backend.)

    # Write the config JSON into the staging dir with a fixed name
    staged_config = staging_dir / "blender_job_config.json"
    staged_config.write_text(json.dumps(cfg_dict))

    # ── Job info file ────────────────────────────────────────────────────────
    job_info_path = staging_dir / "job_info.job"
    plugin_info_path = staging_dir / "plugin_info.job"

    with open(job_info_path, "w") as f:
        write_commandline_job_info(
            f, job, scene_path.name,
            Path(job.video_path).name if job.video_path else "", on_log)

    # ── Plugin info file ─────────────────────────────────────────────────────
    # All paths are fully-qualified absolutes — no CWD ambiguity.
    with open(plugin_info_path, "w") as f:
        f.write(f"Executable={blender_path}\n")
        # Pass the scene via -b only when we have a guaranteed absolute path.
        # The worker script also opens the scene, but Blender's -b is needed
        # to pre-load scene data before the Python script runs.
        # Trailing <STARTFRAME>/<ENDFRAME> let Deadline give each task only its
        # chunk; the worker narrows the rendered range to that (the clip mapping
        # still uses the full range). For a single task they equal the full range,
        # so non-chunked jobs render exactly as before. If a node's plugin doesn't
        # substitute them, the worker ignores the literal tokens and renders full.
        f.write(
            f'Arguments=-b "{scene_arg}"'
            f' --python "{staged_worker}"'
            f' -- "{staged_config}" <STARTFRAME> <ENDFRAME>\n'
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

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=subprocess_creation_flags(),
    )
    try:
        if process.stdout is not None:
            for line in process.stdout:
                if on_log:
                    on_log(line.rstrip())
        return process.wait()
    finally:
        # Never leak the deadlinecommand subprocess if on_log throws mid-stream.
        if process.poll() is None:
            process.kill()


C4D_LICENSE_INPUT = "1\n"   # selects "Maxon App" at c4dpy's license-method prompt


def is_c4d_scene(scene_path: str) -> bool:
    return scene_backend(scene_path) is SceneBackend.C4D


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
        creationflags=subprocess_creation_flags(),
    )
    try:
        if process.stdin:
            process.stdin.write(C4D_LICENSE_INPUT)
            process.stdin.flush()
            process.stdin.close()
        if process.stdout is not None:
            for line in process.stdout:
                if should_cancel and should_cancel():
                    terminate_process(process)
                    break
                if on_log:
                    on_log(line.rstrip())
        rc = process.wait()
    finally:
        if process.poll() is None:
            terminate_process(process)
        config_path.unlink(missing_ok=True)   # don't leak the temp job config

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
    # Route web-native scenes (.glb/.gltf) to the headless three.js backend.
    from .web_render import is_web_scene, run_web_job
    if is_web_scene(job.scene_path):
        return run_web_job(job, on_log, should_cancel)

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

    process = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=subprocess_creation_flags(),
        )

        if process.stdout is not None:
            def _on_timeout(kind: str, secs: float) -> None:
                if on_log:
                    which = "Hard" if kind == "hard" else "Idle (no output)"
                    on_log(f"[app] {which} timeout reached ({secs:g}s), terminating Blender process...")

            def _on_cancel() -> None:
                if on_log:
                    on_log("[app] Cancel requested, stopping Blender process...")

            for line in iter_process_output(
                process,
                hard_timeout=hard_timeout,
                idle_timeout=idle_timeout,
                should_cancel=should_cancel,
                on_timeout=_on_timeout,
                on_cancel=_on_cancel,
            ):
                if on_log:
                    on_log(line)

        return process.wait()
    finally:
        if process is not None and process.poll() is None:
            terminate_process(process)   # ensure no orphan on cancel/timeout/error
        if config_path.exists():
            config_path.unlink(missing_ok=True)
