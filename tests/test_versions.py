from core.utils import latest_by_base, parse_version, reconcile_versions


def test_parse_version_tokens():
    assert parse_version("Screen_v2") == ("screen", 2)
    assert parse_version("Screen_v002") == ("screen", 2)
    assert parse_version("Screen-V3_final".replace("_final", "")) == ("screen", 3)
    assert parse_version("Wall_3") == ("wall", 3)
    assert parse_version("Floor") == ("floor", 0)


def test_latest_by_base_picks_highest_version():
    files = ["/v/Screen_v1.mp4", "/v/Screen_v2.mp4", "/v/Screen_v10.mp4", "/v/Wall.mov"]
    latest = latest_by_base(files)
    assert latest["screen"].endswith("Screen_v10.mp4")
    assert latest["wall"].endswith("Wall.mov")


def test_latest_tie_broken_by_mtime():
    # Same base, version 0 → newest modification time wins.
    latest = latest_by_base(["/a/Shot.mov", "/b/Shot.mov"], mtimes={"/a/Shot.mov": 1.0, "/b/Shot.mov": 2.0})
    assert latest["shot"] == "/b/Shot.mov"


def test_reconcile_adds_new_and_supersedes_old():
    current = ["/v/Screen_v1.mp4", "/v/Logo.png"]
    folder = ["/v/Screen_v1.mp4", "/v/Screen_v2.mp4", "/v/Wall_v1.mp4", "/v/Logo.png"]
    videos, replacements, added = reconcile_versions(current, folder)
    # Screen_v2 supersedes the in-use Screen_v1
    assert replacements == {"/v/Screen_v1.mp4": "/v/Screen_v2.mp4"}
    # Wall is brand new
    assert added == ["/v/Wall_v1.mp4"]
    assert "/v/Screen_v2.mp4" in videos and "/v/Screen_v1.mp4" not in videos
    assert "/v/Wall_v1.mp4" in videos and "/v/Logo.png" in videos


def test_reconcile_no_change_when_already_latest():
    current = ["/v/Screen_v2.mp4"]
    folder = ["/v/Screen_v1.mp4", "/v/Screen_v2.mp4"]
    videos, replacements, added = reconcile_versions(current, folder)
    assert replacements == {}
    assert added == []
    assert videos == ["/v/Screen_v2.mp4"]
