"""Offscreen UI smoke test: construct the whole window and exercise key wiring.
Skipped automatically where PySide6 isn't installed (e.g. the lint job)."""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_window_builds_and_core_wiring(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import app_qt
    # Never touch the real profile/log on disk.
    monkeypatch.setattr(app_qt, "PROFILE_PATH", tmp_path / "p.json")
    monkeypatch.setattr(app_qt, "HISTORY_PATH", tmp_path / "h.json")
    monkeypatch.setattr(app_qt, "LOG_PATH", tmp_path / "l.txt")

    w = app_qt.BlenderVideoMapperQt()
    # Renderer-aware settings swap both ways.
    w.render_panel.set_renderer(True)
    w.render_panel.set_renderer(False)
    # Auto-map + targets wiring.
    w.scene_panel.set_materials(["Screen", "Wall"])
    w.scene_panel.set_videos(["/x/Screen_v1.mp4"])
    assert w.scene_panel._auto_map_by_name(announce=False) == 1
    w.scene_panel.set_targets(["Screen"])
    # Status bar + undo stack exist and update.
    w._update_status_bar()
    assert "Screen" not in w._sb_scene.text()  # no scene loaded
    assert isinstance(w._undo_stack, list)
    app.processEvents()
