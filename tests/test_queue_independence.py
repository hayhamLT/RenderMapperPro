"""Regression guard: queued render jobs are independent snapshots.

The app uses an auto-draft model — one *active* job tracks the live scene, while
other queued jobs are frozen snapshots (``_make_job_snapshot`` deep-copies the
mappings + settings). This locks that contract in: editing the live scene, or
re-mapping, must never reach back and mutate a job that isn't the active one.
A future change that accidentally shares the assignments list by reference (or
collapses the queue to the live scene) fails here.
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
    w = app_qt.BlenderVideoMapperQt()
    w._blender_path = ""                       # no `blender --version` subprocess
    monkeypatch.setattr(w, "_request_auto_preview", lambda *a, **k: None)
    return w


def _map(w, scene: str, material: str, clip: str):
    from core.models import MaterialVideoAssignment
    sp = w.scene_panel
    sp.scene_edit.setText(scene)
    sp.set_materials([material])
    sp.set_assignments([MaterialVideoAssignment(material, clip)])
    w._on_assignments_changed(sp.get_assignments())


def test_non_active_job_survives_later_scene_edits(tmp_path, monkeypatch):
    w = _window(tmp_path, monkeypatch)

    # Map scene A → auto-draft becomes the active job.
    _map(w, "/scenes/A.blend", "Screen", "/clips/A.mp4")
    orig_id = w._active_job_id
    assert orig_id is not None

    # Duplicate: the clone becomes active; the original is now a frozen snapshot.
    w._duplicate_jobs([orig_id])
    assert w._active_job_id != orig_id, "duplicate should activate the clone"

    # Edit the live scene (a different clip) — updates ONLY the active job.
    from core.models import MaterialVideoAssignment
    w.scene_panel.set_assignments([MaterialVideoAssignment("Screen", "/clips/B.mp4")])
    w._on_assignments_changed(w.scene_panel.get_assignments())

    # The original (non-active) job must still point at clip A.
    orig = next(j for j in w._jobs if j.id == orig_id)
    paths = [a.video_path for a in orig.material_assignments]
    assert paths == ["/clips/A.mp4"], f"frozen snapshot was clobbered: {paths}"


def test_duplicate_does_not_share_assignment_objects(tmp_path, monkeypatch):
    w = _window(tmp_path, monkeypatch)
    _map(w, "/scenes/A.blend", "Screen", "/clips/A.mp4")
    src_id = w._active_job_id
    w._duplicate_jobs([src_id])
    src = next(j for j in w._jobs if j.id == src_id)
    clone = next(j for j in w._jobs if j.id == w._active_job_id)

    # Distinct jobs, distinct assignment lists AND distinct element objects —
    # mutating one job's mapping must not bleed into the other.
    assert src is not clone
    assert src.material_assignments is not clone.material_assignments
    assert src.material_assignments[0] is not clone.material_assignments[0]
