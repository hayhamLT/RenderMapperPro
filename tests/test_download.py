"""Tests for the extracted progress-download helper (via a file:// URL)."""
import pytest

from core.download import DownloadCancelled, download_with_progress


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


def test_download_reports_structured_progress(tmp_path):
    """on_progress drives a real progress bar: starts at 0, ends at the full size,
    with a known total (file:// provides Content-Length)."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 100_000)
    dest = tmp_path / "dest.bin"
    seen: list[tuple[int, int]] = []
    download_with_progress(src.as_uri(), dest, on_progress=lambda r, t: seen.append((r, t)),
                           chunk=4096)
    assert seen[0][0] == 0
    assert seen[-1][0] == 100_000
    assert all(total == 100_000 for _read, total in seen)


def test_download_cancellation_raises(tmp_path):
    """should_cancel() returning True aborts mid-stream with DownloadCancelled."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"x" * 100_000)
    dest = tmp_path / "dest.bin"
    with pytest.raises(DownloadCancelled):
        download_with_progress(src.as_uri(), dest, should_cancel=lambda: True, chunk=4096)
