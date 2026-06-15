"""Job-level domain logic — pure, UI-free, unit-testable.

Output-size estimation, disk-space pre-flight warnings, and saved-profile schema
migration, extracted from the Qt window so they can be tested without
instantiating the main window.
"""
from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from .metrics import estimate_output_bytes
from .models import RenderJob
from .utils import ext_for_format

_GB = 1024 ** 3


def estimate_job_bytes(job: RenderJob) -> int:
    """Estimate the on-disk size of a job's output (0 if unknown)."""
    opts = job.render_options
    if not opts:
        return 0
    is_video = bool(ext_for_format(opts.output_format))
    step = max(1, getattr(opts, "frame_step", 1))
    frames = max(1, (opts.frame_end - opts.frame_start) // step + 1)
    return estimate_output_bytes(
        opts.width, opts.height, frames,
        is_video=is_video, quality=getattr(opts, "video_quality", "HIGH"),
        image_format=opts.output_format,
        scale_percent=getattr(opts, "resolution_percentage", 100))


def disk_space_warnings(pending: list[RenderJob]) -> list[str]:
    """Pre-flight warnings for jobs whose output dir may run out of room."""
    warns: list[str] = []
    by_dir: dict[str, int] = {}
    dpaths: dict[str, Path] = {}
    for j in pending:
        out = (j.output_path or "").strip()
        if not out:
            continue
        d = Path(out).expanduser().parent
        while not d.exists() and d.parent != d:
            d = d.parent
        if not d.exists():
            continue
        key = str(d)
        dpaths[key] = d
        by_dir[key] = by_dir.get(key, 0) + estimate_job_bytes(j)
    for key, est in by_dir.items():
        try:
            free = shutil.disk_usage(key).free
        except OSError:
            continue
        if est and free < est * 1.15:
            warns.append(
                f"“{dpaths[key]}” may run out of room: ~{est / _GB:.1f} GB estimated, "
                f"only {free / _GB:.1f} GB free.")
        elif free < 2 * _GB:
            warns.append(f"Low disk space on “{dpaths[key]}”: {free / _GB:.1f} GB free.")
    return warns


def migrate_profile(d: dict, current_version: int,
                    log: Callable[[str], None] | None = None) -> dict:
    """Bring an older saved profile up to ``current_version``. A newer-than-
    current profile loads as-is (best effort) so a downgrade never wipes state.
    This is the scaffold to hang real field migrations on."""
    _log = log or (lambda *_: None)
    try:
        ver = int(d.get("version", 1))
    except (TypeError, ValueError):
        ver = 1
    if ver > current_version:
        _log(f"[app] Settings are from a newer build (v{ver} > v{current_version}); "
             "loading what's compatible.")
        return d
    if ver == current_version:
        return d
    migrated = dict(d)
    # Forward migrations go here, smallest version first.
    migrated["version"] = current_version
    _log(f"[app] Migrated settings from v{ver} to v{current_version}.")
    return migrated
