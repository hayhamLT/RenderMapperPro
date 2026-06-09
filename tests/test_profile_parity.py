"""Profile save/load parity: loading a saved profile and saving again must be
lossless. Catches save/load schema drift (a key written but never read back, or
read but not re-written) — the class of bug that silently resets settings."""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Keys that legitimately differ between two save calls (volatile/runtime state).
VOLATILE = {"layout_state", "layout_geometry"}


def _make_window(app_qt, tmp_path, monkeypatch):
    monkeypatch.setattr(app_qt, "PROFILE_PATH", tmp_path / "profile.json")
    monkeypatch.setattr(app_qt, "HISTORY_PATH", tmp_path / "history.json")
    monkeypatch.setattr(app_qt, "LOG_PATH", tmp_path / "log.txt")
    return app_qt.BlenderVideoMapperQt()


def test_profile_round_trip_is_lossless(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app_qt

    w1 = _make_window(app_qt, tmp_path, monkeypatch)
    # Set distinctive values across subsystems so defaults can't mask drift.
    w1._blender_path = "/opt/blender/blender"
    w1._c4dpy_path = "/opt/maxon/c4dpy"
    w1._deadline_repo_path = "/mnt/repo"
    w1._deadline_command_path = "/opt/dl/deadlinecommand"
    w1._deadline_job_name_template = "X - {scene_name}"
    w1._deadline_comment = "parity"
    w1._autorender_enabled = True
    w1._autorender_start = True
    w1._autorender_output = "/out/previz"
    w1._autorender_pattern = "{clip}_TEST"
    w1._when_done = "sleep"
    w1.scene_panel.set_materials(["Screen", "Wall"])
    w1.scene_panel.set_targets(["Screen"])
    w1.scene_panel.set_watch_options(7000, 5.0)
    d1 = w1._profile_dict()
    w1._save_profile()

    w2 = _make_window(app_qt, tmp_path, monkeypatch)   # loads the saved profile
    d2 = w2._profile_dict()

    drift = {}
    for k in sorted(set(d1) | set(d2)):
        if k in VOLATILE:
            continue
        if d1.get(k) != d2.get(k):
            drift[k] = (d1.get(k), d2.get(k))
    assert not drift, f"profile keys did not survive a save/load round-trip: {drift}"
