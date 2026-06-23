"""Background worker threads (QThread) for the desktop app.

Extracted from app_qt.py so the subprocess-driving threads are isolated,
reusable, and individually testable. Every worker that drives a renderer
subclasses CancellableWorker, so cancellation works the same way everywhere
(closeEvent cancels + waits all of them — a QThread destroyed mid-run aborts
Qt and orphans its headless subprocess).
"""
from __future__ import annotations

import glob
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from core.discovery import discover_scene_elements
from core.logging_setup import get_logger
from core.metrics import FrameTimer, summarize
from core.models import JobConfig
from core.runner import run_blender_job, submit_deadline_job

_log = get_logger(__name__)

# Progress is parsed from the renderers' stdout.
FRAME_RE = [re.compile(r"Fra:(\d+)"), re.compile(r"Frame\s+(\d+)", re.I)]
DISCOVERY_TIMEOUT = 600


def extract_frame_number(line: str) -> int | None:
    """Pull the current frame out of a Blender/C4D log line, if present."""
    for p in FRAME_RE:
        m = p.search(line)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


# Blender (Cycles + EEVEE) reports within-frame progress as "Rendering X / Y
# samples"; parsed to advance the bar *during* a long frame instead of only when
# the frame changes (a heavy first frame otherwise sits at 0% for its whole run).
SAMPLE_RE = re.compile(r"(\d+)\s*/\s*(\d+)\s+samples", re.I)


def extract_sample_fraction(line: str) -> float | None:
    """Within-frame completion (0..1) from a renderer's 'X / Y samples' line, or
    None if the line carries no sample count."""
    m = SAMPLE_RE.search(line)
    if not m:
        return None
    done, total = int(m.group(1)), int(m.group(2))
    if total <= 0:
        return None
    return max(0.0, min(1.0, done / total))


# Renderers carriage-return-update their per-sample/per-sync progress on a single
# "Fra:123 …" line; universal-newline reading splits each refresh into its own
# line, flooding the live log. These are throttled in the log (still parsed for
# progress). Matches Blender ("Fra:1 …") and the web backend ("[web] Fra:1 …").
_TRANSIENT_PROGRESS_RE = re.compile(r"\bFra:\d+")


def is_transient_progress_line(line: str) -> bool:
    return bool(_TRANSIENT_PROGRESS_RE.search(line))


class RenderProgress:
    """Turns a renderer's stdout into (a) a monotonic 0–100 percent that advances
    *within* a frame via sample counts and reaches ~100% on the last frame, and
    (b) a live-log flood gate that throttles the carriage-return progress spam.
    Pure except for an injected clock, so it's unit-testable."""

    def __init__(self, frame_start: int, frame_end: int, *,
                 min_log_interval: float = 1.0) -> None:
        self.fs = frame_start
        self.span = max(1, frame_end - frame_start + 1)
        self.min_log_interval = min_log_interval
        self._frame: int | None = None
        self._frac = 0.0
        self._emitted_pct = -1.0
        self._last_log_t = float("-inf")

    def update(self, line: str, now: float) -> tuple[float | None, bool, int | None]:
        """Feed one stdout line seen at monotonic time ``now``. Returns
        ``(pct, show_in_log, new_frame)``: ``pct`` is the percent to emit (None
        when it hasn't visibly changed), ``show_in_log`` is False for throttled
        progress spam, ``new_frame`` is the frame number when a new frame just
        started (for per-frame timing) else None."""
        frame = extract_frame_number(line)
        pct: float | None = None
        new_frame: int | None = None
        if frame is not None:
            if frame != self._frame:
                self._frame = frame
                self._frac = 0.0
                new_frame = frame
            frac = extract_sample_fraction(line)
            if frac is not None:
                self._frac = max(self._frac, frac)
            done = (frame - self.fs) + self._frac
            p = max(0.0, min(100.0, done / self.span * 100.0))
            # Emit on any visible (0.1%) change so the bar moves smoothly without
            # flooding the UI with identical updates.
            if round(p, 1) != round(self._emitted_pct, 1):
                self._emitted_pct = p
                pct = p
        show = True
        if is_transient_progress_line(line):
            if now - self._last_log_t >= self.min_log_interval:
                self._last_log_t = now
            else:
                show = False
        return pct, show, new_frame


class CancellableWorker(QThread):
    """QThread with a uniform cooperative-cancel flag. Subclasses pass
    ``self.cancelled`` as ``should_cancel`` to the subprocess runner."""

    def __init__(self) -> None:
        super().__init__()
        self._cancel = False

    def request_cancel(self) -> None:
        self._cancel = True

    def cancelled(self) -> bool:
        return self._cancel


class FuncThread(QThread):
    """Runs a plain callable on a managed Qt thread, so fire-and-forget background
    work participates in the app's shutdown wait. A raw daemon thread does not —
    it can emit a Qt signal after the QApplication is gone, which crashes Qt."""

    def __init__(self, fn: Callable[[], None]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        self._fn()


class DiscoveryThread(CancellableWorker):
    discovered = Signal(list, list, dict)
    error = Signal(str)
    log = Signal(str)

    def __init__(self, blender: str, script: str, scene: str,
                 c4dpy: str = "", c4d_script: str = "") -> None:
        super().__init__()
        self.blender = blender
        self.script = script
        self.scene = scene
        self.c4dpy = c4dpy
        self.c4d_script = c4d_script

    def run(self) -> None:
        try:
            mats, cams, settings = discover_scene_elements(
                blender_executable=self.blender,
                discovery_script_path=self.script,
                scene_path=self.scene,
                on_log=self.log.emit,
                hard_timeout_seconds=DISCOVERY_TIMEOUT,
                c4dpy_executable=self.c4dpy,
                c4d_discover_script=self.c4d_script,
            )
            self.discovered.emit(mats, cams, settings)
        except Exception as exc:
            self.error.emit(str(exc))


class RenderThread(CancellableWorker):
    log = Signal(str)
    job_update = Signal(int, str, float)
    job_error = Signal(int, str)
    frame_metrics = Signal(int, int, float, float)  # job_id, frames_done, avg_spf, p95_spf
    all_done = Signal()

    def __init__(self, blender: str, worker: str, entries: list[dict],
                 c4dpy: str = "", c4d_worker: str = "") -> None:
        super().__init__()
        self.blender = blender
        self.worker = worker
        self.entries = entries
        self.c4dpy = c4dpy
        self.c4d_worker = c4d_worker
        self._skip_current = False

    def request_cancel(self) -> None:
        self._cancel = True
        self._skip_current = True

    def request_skip(self) -> None:
        """Skip only the currently running job; continue with remaining."""
        self._skip_current = True

    def _tag_output_colors(self, cfg) -> None:
        """Stream-copy remux that tags rec.709 + faststart on movie outputs, so
        QuickTimes read identically in every player/NLE (untagged movies get
        re-interpreted and shift colours). Fast (-c copy) and best-effort."""
        out = str(getattr(cfg, "output_path", "") or "")
        if getattr(cfg, "use_deadline", False) or Path(out).suffix.lower() not in {".mp4", ".mov"}:
            return
        if not os.path.exists(out):
            return
        from media import find_ffmpeg_tool
        ffmpeg = find_ffmpeg_tool("ffmpeg")
        if not ffmpeg:
            return
        import subprocess

        from core.utils import subprocess_creation_flags
        from media import REC709_FASTSTART_ARGS
        tmp = str(Path(out).with_suffix(".tagging" + Path(out).suffix))
        try:
            r = subprocess.run(
                [ffmpeg, "-y", "-i", out, "-c", "copy", *REC709_FASTSTART_ARGS, tmp],
                capture_output=True, text=True, timeout=300,
                creationflags=subprocess_creation_flags())
            if r.returncode == 0 and os.path.getsize(tmp) > 0:
                os.replace(tmp, out)
                self.log.emit("[app] Output tagged rec.709 (+faststart).")
            else:
                Path(tmp).unlink(missing_ok=True)
        except Exception:
            try:
                Path(tmp).unlink(missing_ok=True)
            except Exception:
                _log.debug("failed to clean up temp tagging file", exc_info=True)

    def _queue_retry(self, entry: dict) -> bool:
        """Re-queue a failed job once (GPU/license hiccups are the common farm
        failure; one automatic retry protects unattended auto-renders). The
        retry runs after the remaining jobs. Skipped on cancel."""
        if self._cancel or entry.get("retried"):
            return False
        retry = dict(entry)
        retry["retried"] = True
        self.entries.append(retry)   # safe: Python list iteration picks up appends
        self.log.emit(f"[app] Job {entry['id']} failed — will retry once after the remaining jobs.")
        return True

    def run(self) -> None:
        for entry in self.entries:
            jid: int = entry["id"]
            cfg: JobConfig = entry["cfg"]

            if self._cancel:
                self.job_update.emit(jid, "cancelled", 0.0)
                continue

            self.job_update.emit(jid, "running", 0.0)
            self.log.emit(f"[app] Job {jid}: {entry.get('label', '')}")

            fs, fe = cfg.render.frame_start, cfg.render.frame_end
            last_error: list[str] = []
            timer = FrameTimer()
            prog = RenderProgress(fs, fe)

            def on_log(line: str, _j: int = jid, _err: list = last_error,
                       _timer: FrameTimer = timer, _prog: RenderProgress = prog) -> None:
                low = line.lower()
                if "error" in low or "traceback" in low or "not found" in low:
                    _err.append(line.strip())
                now = time.monotonic()
                pct, show, new_frame = _prog.update(line, now)
                if show:
                    self.log.emit(line)
                if pct is not None:
                    self.job_update.emit(_j, "running", pct)
                if new_frame is not None and _timer.record(new_frame, now) is not None:
                    s = summarize(_timer.samples)
                    self.frame_metrics.emit(_j, int(s["count"]), s["avg"], s["p95"])

            try:
                if getattr(cfg, "use_deadline", False):
                    rc = submit_deadline_job(
                        blender_executable=self.blender,
                        worker_script_path=self.worker,
                        job=cfg,
                        on_log=on_log,
                        c4dpy_executable=self.c4dpy,
                        c4d_worker_script=self.c4d_worker,
                    )
                else:
                    rc = run_blender_job(
                        blender_executable=self.blender,
                        worker_script_path=self.worker,
                        job=cfg,
                        on_log=on_log,
                        should_cancel=lambda: self._skip_current,
                        c4dpy_executable=self.c4dpy,
                        c4d_worker_script=self.c4d_worker,
                    )
            except Exception as exc:
                self.log.emit(f"[app] ERROR job {jid}: {exc}")
                if self._queue_retry(entry):
                    self.job_update.emit(jid, "running", 0.0)
                else:
                    self.job_error.emit(jid, str(exc))
                    self.job_update.emit(jid, "failed", 0.0)
                self._skip_current = False
                continue

            if self._cancel or self._skip_current:
                self.job_update.emit(jid, "cancelled", 0.0)
            elif rc == 0:
                self._tag_output_colors(cfg)
                self.job_update.emit(jid, "success", 100.0)
            elif self._queue_retry(entry):
                self.job_update.emit(jid, "running", 0.0)
            else:
                reason = last_error[-1] if last_error else f"Blender exited with code {rc}"
                self.job_error.emit(jid, reason)
                self.job_update.emit(jid, "failed", 0.0)
            self._skip_current = self._cancel  # reset per-job flag unless full cancel

        self.all_done.emit()


class PreviewFrameThread(CancellableWorker):
    """Renders a single frame with the current mappings to a temp PNG."""

    log = Signal(str)
    done = Signal(str, str)  # image_path, error

    def __init__(self, blender: str, worker: str, job: JobConfig, out_dir: str,
                 c4dpy: str = "", c4d_worker: str = "") -> None:
        super().__init__()
        self.blender = blender
        self.worker = worker
        self.job = job
        self.out_dir = out_dir
        self.c4dpy = c4dpy
        self.c4d_worker = c4d_worker

    def run(self) -> None:
        try:
            rc = run_blender_job(self.blender, self.worker, self.job, on_log=self.log.emit,
                                 c4dpy_executable=self.c4dpy, c4d_worker_script=self.c4d_worker,
                                 should_cancel=self.cancelled)
            pngs = sorted(glob.glob(os.path.join(self.out_dir, "*.png")))
            if rc == 0 and pngs:
                self.done.emit(pngs[-1], "")
            else:
                self.done.emit("", f"Preview render failed (exit {rc})")
        except Exception as exc:
            self.done.emit("", str(exc))


class ExportBlendThread(CancellableWorker):
    """Runs the worker in prepare mode to bake a standalone .blend (video mapping
    + render settings) for a render farm, instead of rendering."""

    log = Signal(str)
    done = Signal(bool, str)  # ok, path-or-error

    def __init__(self, blender: str, worker: str, job: JobConfig, out_path: str) -> None:
        super().__init__()
        self.blender = blender
        self.worker = worker
        self.job = job
        self.out_path = out_path

    def run(self) -> None:
        try:
            rc = run_blender_job(self.blender, self.worker, self.job, on_log=self.log.emit,
                                 should_cancel=self.cancelled)
            if rc == 0 and os.path.exists(self.out_path):
                self.done.emit(True, self.out_path)
            else:
                self.done.emit(False, f"Export failed (exit {rc})")
        except Exception as exc:
            self.done.emit(False, str(exc))


class DeadlineQueryThread(CancellableWorker):
    """Asks the Deadline repository for pools/groups/machines off the UI thread
    (each call can block for many seconds when the farm is unreachable)."""

    result = Signal(dict)   # {ok, error, pools, groups, machines}

    def __init__(self, deadline_cmd: str, repo_path: str = "") -> None:
        super().__init__()
        self.deadline_cmd = deadline_cmd
        self.repo_path = repo_path

    def _run_cmd(self, flag: str) -> tuple[int, str, str]:
        import subprocess

        from core.utils import subprocess_creation_flags
        if self.repo_path:
            args = [self.deadline_cmd, "RunCommandForRepository", "Direct", self.repo_path, flag]
        else:
            args = [self.deadline_cmd, flag]
        r = subprocess.run(args, capture_output=True, text=True, timeout=8,
                           creationflags=subprocess_creation_flags())
        return r.returncode, r.stdout, r.stderr

    def run(self) -> None:
        out = {"ok": False, "error": "", "pools": [], "groups": [], "machines": []}
        try:
            rc, stdout, stderr = self._run_cmd("-pools")
            if rc != 0:
                out["error"] = (stderr or stdout).strip() or f"deadlinecommand exited with code {rc}"
                self.result.emit(out)
                return
            out["pools"] = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
            rc, stdout, _ = self._run_cmd("-groups")
            if rc == 0:
                out["groups"] = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
            rc, stdout, _ = self._run_cmd("-GetSlaveNames")
            if rc == 0:
                out["machines"] = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
            out["ok"] = True
        except Exception as exc:
            out["error"] = str(exc)
        self.result.emit(out)
