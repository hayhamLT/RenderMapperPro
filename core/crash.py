"""Crash capture and next-launch reporting — pure, UI-free.

Two capture paths feed one report format:

* Unhandled Python exceptions: the UI-thread excepthook (app_qt) calls
  :func:`write_crash_report` in addition to showing its live dialog, so the
  crash survives as a structured file even if the user closes the app.
* Hard native crashes (segfault in Qt/a driver/ffmpeg bindings): nothing
  Python-side runs at crash time, so :func:`enable_fault_capture` points
  ``faulthandler`` at a per-pid dump file up front. On the next launch,
  :func:`collect_faults` turns any dump left behind by a dead process into a
  normal crash report (a clean exit removes its own dump).

Reports are plain-text files in a crash directory. The UI offers to open a
prefilled GitHub issue (:func:`github_issue_url`) — nothing is ever sent
anywhere without the user clicking that link themselves.
"""
from __future__ import annotations

import faulthandler
import os
import platform
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import IO

from core.logging_setup import get_logger

_log = get_logger(__name__)

_FAULT_PREFIX = "fault-"           # fault-<pid>.dump: armed faulthandler target
_REPORT_PREFIX = "crash-"          # crash-<stamp>.txt: a report to surface
_SEEN_SUFFIX = ".seen"             # acknowledged reports keep the file, renamed

_fault_file: IO[str] | None = None
_fault_path: Path | None = None


def _header(version: str, kind: str) -> str:
    return (
        f"Render Mapper Pro crash report\n"
        f"kind: {kind}\n"
        f"version: {version}\n"
        f"os: {platform.platform()}\n"
        f"python: {sys.version.split()[0]}\n"
        f"time: {datetime.now().isoformat(timespec='seconds')}\n"
        f"{'-' * 60}\n"
    )


def write_crash_report(crash_dir: Path, text: str, *, version: str,
                       kind: str = "exception") -> Path | None:
    """Persist a crash as a structured report file; never raises."""
    try:
        crash_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = crash_dir / f"{_REPORT_PREFIX}{stamp}.txt"
        path.write_text(_header(version, kind) + text, encoding="utf-8")
        return path
    except Exception:
        _log.warning("could not write crash report", exc_info=True)
        return None


def enable_fault_capture(crash_dir: Path) -> None:
    """Arm faulthandler into a per-pid dump file so a native crash (which never
    reaches Python) still leaves evidence for the next launch."""
    global _fault_file, _fault_path
    try:
        crash_dir.mkdir(parents=True, exist_ok=True)
        _fault_path = crash_dir / f"{_FAULT_PREFIX}{os.getpid()}.dump"
        _fault_file = _fault_path.open("w", encoding="utf-8")
        faulthandler.enable(file=_fault_file, all_threads=True)
    except Exception:
        _log.warning("could not enable fault capture", exc_info=True)
        _fault_file, _fault_path = None, None


def disable_fault_capture() -> None:
    """Clean-exit path: disarm faulthandler and remove this pid's dump so the
    next launch doesn't mistake it for a crash."""
    global _fault_file, _fault_path
    try:
        if faulthandler.is_enabled():
            faulthandler.disable()
        if _fault_file is not None:
            _fault_file.close()
        if _fault_path is not None and _fault_path.exists():
            _fault_path.unlink()
    except Exception:
        _log.debug("fault-capture cleanup failed", exc_info=True)
    finally:
        _fault_file, _fault_path = None, None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) TERMINATES on Windows — query the process instead.
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def collect_faults(crash_dir: Path, *, version: str) -> list[Path]:
    """Turn fault dumps left by dead processes into crash reports (next-launch
    sweep). Empty leftovers are just removed; live pids are left alone."""
    reports: list[Path] = []
    try:
        dumps = sorted(crash_dir.glob(f"{_FAULT_PREFIX}*.dump"))
    except OSError:
        return reports
    for dump in dumps:
        m = re.fullmatch(rf"{_FAULT_PREFIX}(\d+)\.dump", dump.name)
        if not m or _pid_alive(int(m.group(1))):
            continue
        try:
            text = dump.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                report = write_crash_report(crash_dir, text, version=version,
                                            kind="native-fault")
                if report is not None:
                    reports.append(report)
            dump.unlink()
        except OSError:
            _log.debug("could not sweep fault dump %s", dump, exc_info=True)
    return reports


def pending_reports(crash_dir: Path) -> list[Path]:
    """Unacknowledged crash reports, newest first."""
    try:
        found = [p for p in crash_dir.glob(f"{_REPORT_PREFIX}*.txt")
                 if not p.name.endswith(_SEEN_SUFFIX)]
    except OSError:
        return []
    return sorted(found, reverse=True)


def acknowledge(report: Path) -> None:
    """Mark a report as seen (kept on disk for reference, no longer surfaced)."""
    try:
        report.rename(report.with_name(report.name + _SEEN_SUFFIX))
    except OSError:
        _log.debug("could not acknowledge crash report %s", report, exc_info=True)


def summarize(report: Path, *, limit: int = 200) -> str:
    """One-line human summary for the next-launch dialog: the last traceback
    line (``TypeError: …``) for exceptions, the signal line for native faults."""
    try:
        lines = [ln.strip() for ln in
                 report.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip()]
    except OSError:
        return ""
    for ln in reversed(lines):
        # Exception reports end with "SomeError: message"; fault dumps contain
        # a "Fatal Python error: Segmentation fault"-style line.
        if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception|Interrupt)\b", ln) \
                or ln.startswith("Fatal Python error"):
            return ln[:limit]
    return lines[-1][:limit] if lines else ""


def github_issue_url(repo: str, report: Path, *, version: str,
                     body_limit: int = 5000) -> str:
    """Prefilled new-issue URL. The report tail is inlined (most recent frames
    matter most); clipped to keep the URL within browser/GitHub limits."""
    try:
        text = report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = "(crash report could not be read)"
    if len(text) > body_limit:
        text = "…(truncated)…\n" + text[-body_limit:]
    title = f"Crash: {summarize(report, limit=80) or 'unhandled error'} (v{version})"
    body = (
        "**What were you doing when it crashed?**\n\n_(please describe)_\n\n"
        f"**Crash report** (`{report.name}`):\n\n```\n{text}\n```\n"
    )
    query = urllib.parse.urlencode({"title": title, "body": body})
    return f"https://github.com/{repo}/issues/new?{query}"
