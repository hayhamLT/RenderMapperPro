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
    # Renderer-aware settings swap across every engine.
    for _eng in ("Redshift", "WEB_THREEJS", "BLENDER_EEVEE", "CYCLES"):
        w.render_panel.set_renderer(_eng)
    # Auto-map wiring (mapping a clip implicitly targets the material).
    w.scene_panel.set_materials(["Screen", "Wall"])
    w.scene_panel.set_videos(["/x/Screen_v1.mp4"])
    assert w.scene_panel._auto_map_by_name(announce=False) == 1
    # Status bar + undo stack exist and update.
    w._update_status_bar()
    assert "Screen" not in w._sb_scene.text()  # no scene loaded
    assert isinstance(w._undo_stack, list)
    app.processEvents()


def test_watch_folder_scan_actually_runs(tmp_path, monkeypatch):
    """Regression: _scan_watch_folder must start its worker thread and emit a
    result (a misplaced thread-start once left the watch folder dead), and
    set_watch_ignore_dir must not NameError."""
    import time

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import app_qt
    monkeypatch.setattr(app_qt, "PROFILE_PATH", tmp_path / "p.json")
    monkeypatch.setattr(app_qt, "HISTORY_PATH", tmp_path / "h.json")
    monkeypatch.setattr(app_qt, "LOG_PATH", tmp_path / "l.txt")
    w = app_qt.BlenderVideoMapperQt()
    sp = w.scene_panel
    wf = tmp_path / "watch"
    wf.mkdir()
    (wf / "Screen_v1.mp4").write_bytes(b"x")
    got = {}
    sp._watch_scanned.connect(lambda listing: got.setdefault("listing", listing))
    sp._watch_folder = str(wf)
    sp.set_watch_ignore_dir(str(tmp_path / "out"))   # must not raise NameError
    sp._scan_watch_folder()                          # must start the thread + emit
    for _ in range(60):
        app.processEvents()
        time.sleep(0.02)
        if "listing" in got:
            break
    assert "listing" in got, "watch scan never emitted — thread not started"
    assert any(p.endswith("Screen_v1.mp4") for p, _s, _m, _d in got["listing"])
    assert sp._watch_scanning is False               # flag reset by _apply_watch_scan


def test_batch_output_dedup_and_autorender_defer(tmp_path, monkeypatch):
    """Two queued jobs with identical output paths must be de-duped (no silent
    overwrite), and an auto-render firing mid-render must be deferred, not dropped."""
    from PySide6.QtWidgets import QApplication

    from core.models import MaterialVideoAssignment
    QApplication.instance() or QApplication([])
    import app_qt
    monkeypatch.setattr(app_qt, "PROFILE_PATH", tmp_path / "p.json")
    monkeypatch.setattr(app_qt, "HISTORY_PATH", tmp_path / "h.json")
    monkeypatch.setattr(app_qt, "LOG_PATH", tmp_path / "l.txt")
    w = app_qt.BlenderVideoMapperQt()

    # --- batch dedup ---
    j1, j2, j3 = app_qt.RenderJob(id=1), app_qt.RenderJob(id=2), app_qt.RenderJob(id=3)
    same = str(tmp_path / "out.mp4")
    j1.output_path = j2.output_path = j3.output_path = same
    assert w._resolve_output_conflicts([j1, j2, j3]) is True
    paths = {j1.output_path, j2.output_path, j3.output_path}
    assert len(paths) == 3, f"outputs collided: {paths}"

    # --- auto-render deferral while a render is 'busy' ---
    started = []
    monkeypatch.setattr(w, "_start_render", lambda **k: started.append(k))
    w._autorender_enabled = True
    w._autorender_start = True
    w._is_rendering = True            # pretend a render is running
    w.scene_panel.scene_edit.setText(str(tmp_path / "scene.blend"))
    w.scene_panel.set_materials(["Screen"])
    w._on_target_set_ready([MaterialVideoAssignment("Screen", "/v/Screen_v1.mp4", "EMISSION_FULL_BRIGHT")])
    assert started == [], "auto-render should not start while busy"
    assert len(w._pending_autorender_ids) == 1, "auto-render job must be deferred, not dropped"


def test_first_run_welcome_suppressed_when_headless(tmp_path, monkeypatch):
    """Regression: the first-run welcome modal must NOT block under the offscreen
    platform. A blocking .exec() there has no one to dismiss it and hung the
    headless CI smoke job for ~10 min (then failed) on most releases."""
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    assert app.platformName() == "offscreen"
    import app_qt
    monkeypatch.setattr(app_qt, "PROFILE_PATH", tmp_path / "p.json")
    monkeypatch.setattr(app_qt, "HISTORY_PATH", tmp_path / "h.json")
    monkeypatch.setattr(app_qt, "LOG_PATH", tmp_path / "l.txt")
    w = app_qt.BlenderVideoMapperQt()
    assert w._is_headless() is True
    # Force the first-run condition, then prove the welcome box is skipped: if it
    # weren't gated, _maybe_first_run would call QMessageBox.exec() (and block).
    w._is_first_run = True
    opened = {"exec": False}
    monkeypatch.setattr(QMessageBox, "exec", lambda self: opened.__setitem__("exec", True) or 0)
    w._maybe_first_run()
    assert opened["exec"] is False, "first-run welcome modal not suppressed under offscreen"
