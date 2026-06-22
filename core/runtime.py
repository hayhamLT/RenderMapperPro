"""Managed-Blender runtime locating — pure, UI-free, no Qt.

Where the app's self-downloaded Blender lives and how to find a Blender
executable on disk. Shared by the scene/render discovery path (``_find_blender``
in app_qt) and the installer UI (``RuntimeInstallThread`` in
app_window/runtime_mixin) so the two never drift. The download + extract logic
lives with the installer; only the *paths* and the *version* live here.
"""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

RUNTIME_ROOT = Path.home() / ".blender_video_mapper" / "runtime"
BLENDER_RUNTIME_VERSION = "5.1.0"


def _runtime_download_spec() -> tuple[str, str] | None:
    """(download URL, archive filename) for this OS/arch, or None where the
    managed runtime isn't supported."""
    v = BLENDER_RUNTIME_VERSION
    parts = v.split(".")
    release_train = ".".join(parts[:2]) if len(parts) >= 2 else v
    base = f"https://download.blender.org/release/Blender{release_train}/blender-{v}"
    machine = platform.machine().lower()

    if sys.platform == "darwin":
        arch = "arm64" if "arm" in machine or "aarch64" in machine else "x64"
        name = f"blender-{v}-macos-{arch}.dmg"
        return f"{base}-macos-{arch}.dmg", name
    if os.name == "nt":
        name = f"blender-{v}-windows-x64.zip"
        return f"{base}-windows-x64.zip", name
    if sys.platform.startswith("linux"):
        arch = "arm64" if "arm" in machine or "aarch64" in machine else "x64"
        name = f"blender-{v}-linux-{arch}.tar.xz"
        return f"{base}-linux-{arch}.tar.xz", name
    return None


def _runtime_checksum_url() -> str | None:
    """URL of blender.org's published ``.sha256`` sidecar, which lists the
    SHA-256 of every platform archive for this release as ``<digest>  <file>``
    lines. Used to verify the managed-runtime download before extracting."""
    if _runtime_download_spec() is None:
        return None
    v = BLENDER_RUNTIME_VERSION
    parts = v.split(".")
    release_train = ".".join(parts[:2]) if len(parts) >= 2 else v
    return f"https://download.blender.org/release/Blender{release_train}/blender-{v}.sha256"


def _norm_blender(candidate: str) -> str | None:
    """Normalise a user/auto candidate to a runnable Blender executable path
    (resolving a macOS .app bundle, an existing file, or a PATH lookup)."""
    candidate = candidate.strip()
    if not candidate:
        return None
    exp = Path(os.path.expanduser(candidate))
    if exp.suffix.lower() == ".app":
        for p in (exp / "Contents/MacOS/Blender", exp / "Contents/MacOS/blender"):
            if p.exists() and p.is_file():
                return str(p)
    if exp.exists() and exp.is_file():
        return str(exp)
    return shutil.which(candidate)


def _managed_blender_executable() -> str | None:
    """The executable inside the managed runtime's ``current`` dir, or None if no
    managed runtime is installed."""
    current = RUNTIME_ROOT / "current"
    if not current.exists():
        return None

    if sys.platform == "darwin":
        cands = [
            current / "Blender.app" / "Contents" / "MacOS" / "Blender",
            current / "Contents" / "MacOS" / "Blender",
        ]
        for c in cands:
            if c.exists() and c.is_file():
                return str(c)
    elif os.name == "nt":
        for c in current.rglob("blender.exe"):
            if c.exists() and c.is_file():
                return str(c)
    else:
        for c in current.rglob("blender"):
            if c.exists() and c.is_file() and os.access(str(c), os.X_OK):
                return str(c)
    return None
