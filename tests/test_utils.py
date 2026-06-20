import os
import re
import subprocess
import sys

from core.utils import (
    atomic_write_text,
    expand_output_tokens,
    ext_for_format,
    is_cloud_placeholder,
    resolve_output_path,
    slugify_filename,
    terminate_process,
)


def test_atomic_write_creates_and_replaces(tmp_path):
    p = tmp_path / "sub" / "f.json"
    atomic_write_text(p, '{"a": 1}')        # also creates the parent dir
    assert p.read_text() == '{"a": 1}'
    atomic_write_text(p, '{"a": 2}')        # replaces in place
    assert p.read_text() == '{"a": 2}'
    assert not (p.parent / "f.json.tmp").exists()   # temp cleaned up by rename


def test_atomic_write_mode(tmp_path):
    p = tmp_path / "secret.json"
    atomic_write_text(p, "x", mode=0o600)
    assert (os.stat(p).st_mode & 0o777) == 0o600


def test_atomic_write_no_partial_on_replace(tmp_path):
    # The original survives intact until the new content is fully written.
    p = tmp_path / "f.txt"
    atomic_write_text(p, "original")
    atomic_write_text(p, "x" * 100000)
    assert p.read_text() == "x" * 100000


class _FakeStat:
    def __init__(self, size, blocks=None, attrs=None):
        self.st_size = size
        if blocks is not None:
            self.st_blocks = blocks
        if attrs is not None:
            self.st_file_attributes = attrs


def test_cloud_placeholder_local_file_is_not_flagged():
    # Fully-allocated file: blocks cover the size → local, ingest it.
    st = _FakeStat(size=1_000_000, blocks=2048)   # 2048*512 = ~1MB
    assert is_cloud_placeholder("x.mp4", st) is False


def test_cloud_placeholder_dataless_unix_is_flagged():
    # Logical size present but almost no blocks on disk → online-only placeholder.
    st = _FakeStat(size=1_000_000, blocks=0)
    assert is_cloud_placeholder("x.mp4", st) is True


def test_cloud_placeholder_windows_offline_attr_is_flagged():
    # Windows OneDrive/Dropbox OFFLINE attribute (0x1000), blocks irrelevant.
    st = _FakeStat(size=1_000_000, blocks=2048, attrs=0x1000)
    assert is_cloud_placeholder("x.mp4", st) is True


def test_cloud_placeholder_empty_file_not_flagged():
    st = _FakeStat(size=0, blocks=0)
    assert is_cloud_placeholder("x.mp4", st) is False


def test_slugify_strips_unsafe_chars():
    s = slugify_filename("a b/c:d*.mp4")
    for bad in (" ", "/", ":", "*"):
        assert bad not in s


def test_slugify_empty_falls_back():
    assert slugify_filename("") == "output"
    assert slugify_filename("...") == "output"


def test_expand_tokens_basic():
    assert expand_output_tokens("{scene}_{video}", "Venue", "clip") == "Venue_clip"


def test_expand_tokens_date():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", expand_output_tokens("{date}", "s", "v"))


def test_expand_tokens_extra():
    assert expand_output_tokens("{camera}", "s", "v", {"camera": "Cam_1"}) == "Cam_1"


def test_ext_for_format():
    assert ext_for_format("MPEG4") == ".mp4"
    assert ext_for_format("png") == ""          # image sequence → folder
    assert ext_for_format("UNKNOWN") == ".mp4"  # safe default


def test_resolve_output_mp4(tmp_path):
    out = resolve_output_path(
        str(tmp_path / "render.mp4"), "/x/Venue.blend", "/x/clip.mp4",
        is_batch=False, output_format="MPEG4",
    )
    assert out.endswith(".mp4")


def test_resolve_output_corrects_extension(tmp_path):
    out = resolve_output_path(
        str(tmp_path / "render.mov"), "/x/Venue.blend", "/x/clip.mp4",
        is_batch=False, output_format="MPEG4",
    )
    assert out.endswith(".mp4")


def test_resolve_output_png_is_directory(tmp_path):
    out = resolve_output_path(
        str(tmp_path / "seq"), "/x/Venue.blend", "/x/clip.mp4",
        is_batch=False, output_format="PNG",
    )
    assert os.path.isdir(out)


def test_terminate_process_escalates_to_kill():
    # A child that ignores SIGTERM must still be stopped (via SIGKILL) promptly.
    code = ("import signal, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "time.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-c", code])
    terminate_process(p, grace=2.0)
    assert p.poll() is not None   # actually dead, not orphaned


def test_terminate_process_already_exited_is_noop():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    terminate_process(p)          # must not raise on an already-dead process
    assert p.poll() is not None
