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

from PySide6.QtCore import QThread, Signal

from core.discovery import discover_scene_elements
from core.models import JobConfig
from core.runner import run_blender_job, submit_deadline_job

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
            span = max(1, fe - fs + 1)
            last_error: list[str] = []

            def on_log(line: str, _j: int = jid, _fs: int = fs, _span: int = span, _err: list = last_error) -> None:
                self.log.emit(line)
                low = line.lower()
                if "error" in low or "traceback" in low or "not found" in low:
                    _err.append(line.strip())
                frame = extract_frame_number(line)
                if frame is not None:
                    pct = max(0.0, min(100.0, ((frame - _fs) / _span) * 100.0))
                    self.job_update.emit(_j, "running", pct)

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
                self.job_error.emit(jid, str(exc))
                self.job_update.emit(jid, "failed", 0.0)
                self._skip_current = False
                continue

            if self._cancel or self._skip_current:
                self.job_update.emit(jid, "cancelled", 0.0)
            elif rc == 0:
                self.job_update.emit(jid, "success", 100.0)
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
