"""Tests for the extracted progress-download helper (via a file:// URL)."""
from core.download import download_with_progress


def test_download_with_progress_copies_bytes(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 100_000)
    dest = tmp_path / "dest.bin"
    logs: list[str] = []
    n = download_with_progress(src.as_uri(), dest, logs.append, chunk=4096)
    assert n == 100_000
    assert dest.read_bytes() == src.read_bytes()
    # file:// exposes Content-Length, so progress lines are emitted.
    assert any("Download" in m and "%" in m for m in logs)
