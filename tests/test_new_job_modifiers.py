"""The queue's New-job (+) button is modifier-aware:

  • plain click       → queue the current setup (covered elsewhere)
  • Shift+click       → queue, then empty the Videos section (keep the scene)
  • Ctrl/⌘+Shift+click → start a brand-new empty job (no scene, no videos), but
                         leave the already-queued jobs intact

These lock in the two new handlers so a future refactor can't silently collapse
the distinction (e.g. clobber the just-queued snapshot, or wipe the queue).
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app_qt
    monkeypatch.setattr(app_qt, "PROFILE_PATH", tmp_path / "p.json")
    monkeypatch.setattr(app_qt, "HISTORY_PATH", tmp_path / "h.json")
    monkeypatch.setattr(app_qt, "LOG_PATH", tmp_path / "l.txt")
    # queue_mixin now shows the app-styled ui_dialogs helpers (not QMessageBox);
    # stub them so an offscreen modal can't block the run (confirm → proceed).
    monkeypatch.setattr("app_window.queue_mixin.inform", lambda *a, **k: None)
    monkeypatch.setattr("app_window.queue_mixin.warn", lambda *a, **k: None)
    monkeypatch.setattr("app_window.queue_mixin.confirm", lambda *a, **k: True)
    w = app_qt.BlenderVideoMapperQt()
    w._blender_path = ""                       # no `blender --version` subprocess
    monkeypatch.setattr(w, "_request_auto_preview", lambda *a, **k: None)
    return w


def _map(w, scene: str, material: str, clip: str):
    from core.models import MaterialVideoAssignment
    sp = w.scene_panel
    sp.scene_edit.setText(scene)
    sp.set_materials([material])
    sp.set_videos([clip])
    sp.set_assignments([MaterialVideoAssignment(material, clip)])
    w._on_assignments_changed(sp.get_assignments())


def test_shift_new_queues_then_empties_videos(tmp_path, monkeypatch):
    w = _window(tmp_path, monkeypatch)
    _map(w, "/scenes/A.blend", "Screen", "/clips/A.mp4")
    n_before = len(w._jobs)
    assert n_before >= 1

    w._queue_current_jobs_clear_videos()        # Shift+click

    # A job was committed to the queue.
    assert len(w._jobs) == n_before + 1
    # The 3D scene stays loaded, but the Videos section (and its mappings) empties.
    sp = w.scene_panel
    assert sp.scene_edit.text() == "/scenes/A.blend"
    assert sp.get_videos() == []
    assert sp.get_assignments() == []
    # The workspace detaches so the next edit can't reach back into the queued job.
    assert w._active_job_id is None
    # The committed snapshot kept its clip.
    assert any(a.video_path == "/clips/A.mp4"
               for j in w._jobs for a in j.material_assignments)


def test_shift_new_with_nothing_queued_leaves_workspace(tmp_path, monkeypatch):
    """No scene/clips → nothing to queue → the workspace is left untouched."""
    w = _window(tmp_path, monkeypatch)
    assert w._jobs == []

    w._queue_current_jobs_clear_videos()

    assert w._jobs == []
    assert w.scene_panel.get_videos() == []


def test_ctrl_shift_new_blank_keeps_queue(tmp_path, monkeypatch):
    w = _window(tmp_path, monkeypatch)
    _map(w, "/scenes/A.blend", "Screen", "/clips/A.mp4")
    n_jobs = len(w._jobs)
    assert n_jobs >= 1

    w._new_blank_job()                          # Ctrl/⌘+Shift+click

    # The whole workspace is blank — no scene (3D object), no videos, no mappings.
    sp = w.scene_panel
    assert sp.scene_edit.text() == ""
    assert sp.get_videos() == []
    assert sp.get_assignments() == []
    assert w._active_job_id is None
    # ...but the existing queue is untouched.
    assert len(w._jobs) == n_jobs
    assert any(a.video_path == "/clips/A.mp4"
               for j in w._jobs for a in j.material_assignments)
