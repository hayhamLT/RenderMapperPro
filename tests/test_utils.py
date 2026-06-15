import os
import re
import subprocess
import sys

from core.utils import (
    expand_output_tokens,
    ext_for_format,
    resolve_output_path,
    slugify_filename,
    terminate_process,
)


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
