from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence

from test_watch_wiring import _make_window


def test_presets_watcher_initialized(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    assert hasattr(w, "_preset_watcher")
    assert w._preset_watcher is not None


def test_pair_table_editor_delete_shortcut(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    wp = w.watch_panel
    table = wp.screen_table.table

    # Find the delete QAction added to the table
    actions = table.actions()
    del_act = next((a for a in actions if a.text() == "Delete Selected"), None)
    assert del_act is not None
    assert len(del_act.shortcuts()) == 2
    assert del_act.shortcuts()[0] == QKeySequence(Qt.Key.Key_Delete)
    assert del_act.shortcuts()[1] == QKeySequence(Qt.Key.Key_Backspace)


def test_dock_layout_change_triggers_save(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)

    # Reset any pending save timer
    w._save_timer = None

    # Trigger a dock visibility change (or directly call the slot)
    w._on_dock_layout_changed()

    # Verify that save was scheduled (timer was started)
    assert w._save_timer is not None
    assert w._save_timer.isActive() is True
