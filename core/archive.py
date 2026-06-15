"""Safe archive extraction + download-integrity helpers.

``zipfile``/``tarfile`` ``extractall`` follow member paths verbatim, so a crafted
archive can write *outside* the destination (Zip-Slip / path traversal). These
helpers reject any member that escapes the target dir, and verify a SHA-256 when
one is known. Use them for every downloaded/extracted artifact (runtime, ffmpeg,
self-update).
"""
from __future__ import annotations

import hashlib
import tarfile
import zipfile
from pathlib import Path


def _is_within(base: Path, target: Path) -> bool:
    """True if ``target`` resolves to a path inside ``base`` (no traversal)."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def safe_extract_zip(zip_path: str | Path, dest: str | Path) -> None:
    """Extract a zip, refusing any member that would escape ``dest``."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if not _is_within(dest, dest / name):
                raise RuntimeError(f"Unsafe path in archive (Zip-Slip): {name!r}")
        zf.extractall(dest)


def safe_extract_tar(tar_path: str | Path, dest: str | Path) -> None:
    """Extract a tar, using the 3.12 ``data`` filter (rejects traversal/special
    members), with a manual fallback for older interpreters."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:   # interpreter predates the filter argument
            for member in tf.getmembers():
                if not _is_within(dest, dest / member.name):
                    raise RuntimeError(f"Unsafe path in archive (Zip-Slip): {member.name!r}") from None
            tf.extractall(dest)


def sha256_file(path: str | Path) -> str:
    """Streaming SHA-256 of a file (constant memory)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: str | Path, expected: str) -> None:
    """Raise if the file's SHA-256 doesn't match ``expected`` (case-insensitive)."""
    actual = sha256_file(path)
    if actual.lower() != expected.lower():
        raise RuntimeError(
            f"Checksum mismatch for {Path(path).name}: expected {expected}, got {actual}")
