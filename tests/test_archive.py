"""Tests for the Zip-Slip-safe extraction + checksum helpers."""
import hashlib
import io
import tarfile
import zipfile

import pytest

from core.archive import (
    expected_sha256_from_sidecar,
    safe_extract_tar,
    safe_extract_zip,
    sha256_file,
    verify_sha256,
)

# A real-shaped blender.org .sha256 sidecar (digest, two spaces, filename).
_SIDECAR = (
    "ebeda0dbb7fbf06f3cbcf58c11397551de745f1177acfe3e2331ac0d4d901996  blender-5.1.0-macos-arm64.dmg\n"
    "7f2475990613c8d4c7ac5697803fcf40d09541c1fd8c23936f4b07a169a920c7  blender-5.1.0-linux-x64.tar.xz\n"
    "bc184226962904e3f26c5809ba7ec86bbeb670060825b046930b0c02a30a4eee  blender-5.1.0-windows-x64.zip\n"
)


def test_sidecar_match_by_basename():
    assert (expected_sha256_from_sidecar(_SIDECAR, "blender-5.1.0-windows-x64.zip")
            == "bc184226962904e3f26c5809ba7ec86bbeb670060825b046930b0c02a30a4eee")
    # a full path still matches on basename
    assert expected_sha256_from_sidecar(_SIDECAR, "/tmp/dl/blender-5.1.0-macos-arm64.dmg")


def test_sidecar_missing_and_noise():
    assert expected_sha256_from_sidecar(_SIDECAR, "blender-9.9.9-macos-arm64.dmg") is None
    assert expected_sha256_from_sidecar("# comment\n\n   \n", "x.zip") is None


def test_sidecar_feeds_verify(tmp_path):
    f = tmp_path / "blender-5.1.0-windows-x64.zip"
    f.write_bytes(b"hello")
    digest = sha256_file(f)
    sidecar = f"{digest}  blender-5.1.0-windows-x64.zip\n"
    expected = expected_sha256_from_sidecar(sidecar, f.name)
    assert expected is not None
    verify_sha256(f, expected)   # must not raise
    with pytest.raises(RuntimeError):
        verify_sha256(f, "0" * 64)


def test_safe_extract_zip_ok(tmp_path):
    z = tmp_path / "a.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("sub/file.txt", "hi")
    dest = tmp_path / "out"
    safe_extract_zip(z, dest)
    assert (dest / "sub" / "file.txt").read_text() == "hi"


def test_safe_extract_zip_rejects_traversal(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../escape.txt", "pwned")
    with pytest.raises(RuntimeError):
        safe_extract_zip(z, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_tar_rejects_traversal(tmp_path):
    t = tmp_path / "evil.tar"
    with tarfile.open(t, "w") as tf:
        data = b"pwned"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises((RuntimeError, tarfile.TarError)):
        safe_extract_tar(t, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_sha256_and_verify(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"hello")
    digest = hashlib.sha256(b"hello").hexdigest()
    assert sha256_file(f) == digest
    verify_sha256(f, digest)
    verify_sha256(f, digest.upper())          # case-insensitive
    with pytest.raises(RuntimeError):
        verify_sha256(f, "0" * 64)
