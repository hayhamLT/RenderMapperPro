"""Tests for the Zip-Slip-safe extraction + checksum helpers."""
import hashlib
import io
import tarfile
import zipfile

import pytest

from core.archive import (
    safe_extract_tar,
    safe_extract_zip,
    sha256_file,
    verify_sha256,
)


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
