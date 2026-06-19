"""Clean launch + New + Reopen Last Session behaviour."""


def _make_window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app_qt
    monkeypatch.setattr(app_qt, "PROFILE_PATH", tmp_path / "p.json")
    monkeypatch.setattr(app_qt, "HISTORY_PATH", tmp_path / "h.json")
    monkeypatch.setattr(app_qt, "LOG_PATH", tmp_path / "l.txt")
    return app_qt.BlenderVideoMapperQt()


def _seed_session(w):
    from core.models import RenderJob
    w.scene_panel.scene_edit.setText("/x/Stage.blend")
    w._jobs = [RenderJob(id=1, scene_path="/x/Stage.blend", label="job")]
    w._active_job_id = 1


def test_new_session_clears_workspace(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    _seed_session(w)
    w._new_session(confirm=False, announce=False)
    assert w.scene_panel.scene_edit.text() == ""
    assert w._jobs == []
    assert w._active_job_id is None


def test_clean_launch_empties_but_stashes(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    _seed_session(w)
    d = w._profile_dict()                 # restore_session_on_launch defaults False
    assert d["restore_session_on_launch"] is False
    w._apply_profile_data(d)              # simulate a clean launch from this profile
    assert w.scene_panel.scene_edit.text() == "", "clean launch should open empty"
    assert w._jobs == []
    assert w._last_session.get("scene") == "/x/Stage.blend", "previous session stashed"


def test_reopen_last_session_restores(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    _seed_session(w)
    w._apply_profile_data(w._profile_dict())   # clean launch → cleared + stashed
    assert w._jobs == []
    w._reopen_last_session()
    assert w.scene_panel.scene_edit.text() == "/x/Stage.blend"
    assert [j.id for j in w._jobs] == [1]


def test_restore_on_launch_keeps_session(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    _seed_session(w)
    w._restore_session_on_launch = True
    d = w._profile_dict()
    assert d["restore_session_on_launch"] is True
    w._apply_profile_data(d)
    assert w.scene_panel.scene_edit.text() == "/x/Stage.blend", "restore mode keeps the session"
    assert [j.id for j in w._jobs] == [1]
