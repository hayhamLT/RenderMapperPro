"""App-level behavior tests (offscreen). Covers profile persistence, the render
preflight validations, and the user-facing classifiers — the orchestration layer
the smoke test only constructs."""
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
    return app_qt, app_qt.BlenderVideoMapperQt()


def test_properties_dialog_builds(tmp_path, monkeypatch):
    """The extracted Properties dialog builds every tab against the live window
    without error — catches a broken self->win reference or a missing attribute."""
    _app_qt, w = _window(tmp_path, monkeypatch)
    w._blender_path = ""   # avoid a `blender --version` subprocess at build time
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, "exec", lambda self: 0)   # don't block on the modal loop
    import dialogs
    dialogs.build_properties_dialog(w)


def test_help_dialogs_build_and_links_route(tmp_path, monkeypatch):
    """The rich-text help dialogs build without error, and the interactive
    ``action:`` links route to the right in-app dialog while ``http`` links go
    to the system browser — guards the click-through help wiring."""
    _app_qt, w = _window(tmp_path, monkeypatch)
    w._blender_path = ""   # avoid a `blender --version` subprocess in _show_about/quick-start
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QDialog, QMessageBox
    monkeypatch.setattr(QDialog, "exec", lambda self: 0)   # don't block on the modals
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)

    # Each help surface builds and shows without raising.
    w._show_quick_start()
    w._show_shortcuts_help()
    w._show_about()

    # action: anchors open the matching dialog, passing any tab argument through.
    opened: dict = {}
    monkeypatch.setattr(w, "_show_properties_dialog", lambda tab=None: opened.update(props=tab))
    monkeypatch.setattr(w, "_show_history_dialog", lambda: opened.update(history=True))
    w._on_help_anchor(QUrl("action:properties/Updates"), None)
    w._on_help_anchor(QUrl("action:history"), None)
    assert opened == {"props": "Updates", "history": True}

    # http(s) anchors go to the browser, never an in-app action.
    visited: list = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: visited.append(url.toString()))
    w._on_help_anchor(QUrl("https://example.com/docs"), None)
    assert visited == ["https://example.com/docs"]


def test_glb_offers_blender_and_threejs_engines(tmp_path, monkeypatch):
    """A .glb scene must offer Blender engines alongside three.js (Blender can
    import + render the glTF), with three.js the default."""
    _app_qt, w = _window(tmp_path, monkeypatch)
    w._set_renderer_options(is_c4d=False, is_web=True)
    engines = w.render_panel.engine_values()
    assert "WEB_THREEJS" in engines
    assert "CYCLES" in engines
    assert "BLENDER_EEVEE" in engines
    assert w.render_panel.engine_value() == "WEB_THREEJS"   # three.js default


def test_profile_roundtrip(tmp_path, monkeypatch):
    """State written by _profile_dict must be read back by _apply_profile_data."""
    app_qt, w = _window(tmp_path, monkeypatch)
    w._theme_mode = "light"
    w._when_done = "quit"
    w._blender_path = "/opt/blender/blender"
    w.scene_panel.scene_edit.setText("/proj/scene.blend")
    data = w._profile_dict()

    w2 = app_qt.BlenderVideoMapperQt()
    w2._apply_profile_data(data)
    assert w2._theme_mode == "light"
    assert w2._when_done == "quit"
    assert w2._blender_path == "/opt/blender/blender"
    assert w2.scene_panel.scene_edit.text() == "/proj/scene.blend"


def test_profile_migration(tmp_path, monkeypatch):
    app_qt, w = _window(tmp_path, monkeypatch)
    cur = app_qt.PROFILE_VERSION
    assert w._migrate_profile({"version": cur}) == {"version": cur}
    assert w._migrate_profile({"version": 1})["version"] == cur     # upgraded
    assert w._migrate_profile({"version": 999})["version"] == 999   # newer kept as-is
    assert w._migrate_profile({})["version"] == cur                 # missing → migrate


def test_deadline_warnings(tmp_path, monkeypatch):
    app_qt, w = _window(tmp_path, monkeypatch)
    w.deadline_panel.dl_pool_combo.addItems(["render_pool"])
    w.deadline_panel.dl_group_combo.addItems(["", "gpu"])
    job = app_qt.RenderJob(id=1)
    job.use_deadline = True
    job.deadline_pool = "ghost_pool"
    assert any("ghost_pool" in m for m in w._deadline_warnings([job]))
    job.deadline_pool = "render_pool"
    assert w._deadline_warnings([job]) == []
    job.use_deadline = False   # non-deadline jobs are never flagged
    job.deadline_pool = "ghost_pool"
    assert w._deadline_warnings([job]) == []


def test_friendly_error_hint(tmp_path, monkeypatch):
    _app_qt, w = _window(tmp_path, monkeypatch)
    assert "memory" in w._friendly_error_hint("CUDA error: out of memory").lower()
    assert "disk" in w._friendly_error_hint("Errno 28 No space left on device").lower()
    assert "codec" in w._friendly_error_hint("Unknown encoder 'libx264'").lower()
    assert w._friendly_error_hint("nondescript blip") == ""


def test_sheet_path_for(tmp_path, monkeypatch):
    _app_qt, w = _window(tmp_path, monkeypatch)
    assert str(w._sheet_path_for("/x/out.mp4")).endswith("out_contactsheet.png")
    assert str(w._sheet_path_for("/x/seq")).endswith("seq/_contactsheet.png")
    assert w._sheet_path_for("") is None


def test_command_actions(tmp_path, monkeypatch):
    _app_qt, w = _window(tmp_path, monkeypatch)
    actions = w._command_actions()
    assert len(actions) >= 10
    assert all(isinstance(name, str) and name for name, _fn in actions)
    assert all(callable(fn) for _name, fn in actions)
    # Labels are unique (the palette maps by label).
    names = [n for n, _ in actions]
    assert len(names) == len(set(names))


def test_power_cost_roundtrips(tmp_path, monkeypatch):
    app_qt, w = _window(tmp_path, monkeypatch)
    w._power_watts = 450.0
    w._power_rate = 0.22
    w2 = app_qt.BlenderVideoMapperQt()
    w2._apply_profile_data(w._profile_dict())
    assert w2._power_watts == 450.0
    assert w2._power_rate == 0.22


def test_notify_settings_roundtrip(tmp_path, monkeypatch):
    app_qt, w = _window(tmp_path, monkeypatch)
    w._notify_desktop = False
    w._discord_webhook = "https://discord.com/api/webhooks/abc"
    w2 = app_qt.BlenderVideoMapperQt()
    w2._apply_profile_data(w._profile_dict())
    assert w2._notify_desktop is False
    assert w2._discord_webhook == "https://discord.com/api/webhooks/abc"


def test_requeue_jobs(tmp_path, monkeypatch):
    app_qt, w = _window(tmp_path, monkeypatch)
    job = app_qt.RenderJob(id=1)
    job.status = "failed"
    job.error = "boom"
    job.progress = 100.0
    job.selected = False
    w._jobs = [job]
    w._requeue_jobs([1])
    assert job.status == "idle"
    assert job.selected is True
    assert job.error == ""
    assert job.progress == 0.0


def test_chunk_target_minutes(tmp_path, monkeypatch):
    _app_qt, w = _window(tmp_path, monkeypatch)
    dp = w.deadline_panel
    dp.dl_chunk_strategy.setCurrentIndex(0)   # Manual
    assert dp.chunk_target_minutes() == 0.0
    dp.dl_chunk_strategy.setCurrentIndex(2)   # ~10 min
    assert dp.chunk_target_minutes() == 10.0
