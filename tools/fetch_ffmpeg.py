#!/usr/bin/env python3
"""Download static ffmpeg + ffprobe for the *current* platform into
``vendor/ffmpeg/<platform-arch>/`` so PyInstaller can bundle them.

Cross-platform (macOS / Windows / Linux) so the same step works on every CI
runner and on a developer machine. Mirrors the resolution the app uses at
runtime (see ``_ffmpeg_platform_dir`` / ``find_ffmpeg_tool`` in app_qt.py).

Source: eugeneware/ffmpeg-static GitHub releases. The asset names line up
exactly with our platform-dir convention, e.g. ``ffmpeg-darwin-arm64``,
``ffmpeg-win32-x64``, ``ffprobe-linux-x64``. Override the source with the
FFMPEG_STATIC_BASE / FFMPEG_STATIC_VERSION env vars.

These are GPL static builds (~43 MB each) — distributing them carries GPL
obligations.
"""
from __future__ import annotations

import os
import platform
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

VERSION = os.environ.get("FFMPEG_STATIC_VERSION", "b6.0")
BASE = os.environ.get(
    "FFMPEG_STATIC_BASE",
    f"https://github.com/eugeneware/ffmpeg-static/releases/download/{VERSION}",
)


def platform_dir() -> str:
    """e.g. 'darwin-arm64' | 'win32-x64' | 'linux-x64' — matches app_qt.py."""
    sysn = "linux" if sys.platform.startswith("linux") else sys.platform  # darwin|win32|linux
    mach = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(mach, mach)
    return f"{sysn}-{arch}"


def download(url: str, dest: Path) -> None:
    print(f"  fetching {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "fetch-ffmpeg"})
    with urllib.request.urlopen(req) as resp, open(dest, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)


def main() -> int:
    platdir = platform_dir()
    is_win = sys.platform == "win32"
    out_dir = Path("vendor") / "ffmpeg" / platdir
    out_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for tool in ("ffmpeg", "ffprobe"):
        dest = out_dir / (tool + (".exe" if is_win else ""))
        if dest.exists() and dest.stat().st_size > 0:
            print(f"{tool}: already present at {dest} — skipping")
            continue
        url = f"{BASE}/{tool}-{platdir}"
        try:
            download(url, dest)
        except Exception as exc:
            print(f"WARNING: could not download {tool} ({url}): {exc}", file=sys.stderr)
            dest.unlink(missing_ok=True)
            failures += 1
            continue

        if not is_win:
            dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        if sys.platform == "darwin":
            # Strip quarantine and ad-hoc sign so the binary runs on Apple Silicon.
            subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(dest)], check=False)
            subprocess.run(["codesign", "--force", "-s", "-", str(dest)],
                           check=False, capture_output=True)
        print(f"{tool}: ready at {dest}")

    if failures:
        print(f"Done with {failures} failure(s) — app will fall back to a system "
              "ffmpeg / its built-in MP4 parser for any missing tool.", file=sys.stderr)
    return 0  # never fail the build over ffmpeg; bundling is best-effort


if __name__ == "__main__":
    raise SystemExit(main())
