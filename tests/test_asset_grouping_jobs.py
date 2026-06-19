"""Integration: asset-grouping watch clips → previz render jobs in the app."""


def _make_window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app_qt
    from core.asset_grouping import GroupingConfig
    monkeypatch.setattr(app_qt, "PROFILE_PATH", tmp_path / "p.json")
    monkeypatch.setattr(app_qt, "HISTORY_PATH", tmp_path / "h.json")
    monkeypatch.setattr(app_qt, "LOG_PATH", tmp_path / "l.txt")
    w = app_qt.BlenderVideoMapperQt()
    w.scene_panel.scene_edit.setText(str(tmp_path / "Stage.blend"))
    w._asset_grouping = GroupingConfig(enabled=True)
    w._sync_grouping_mode()
    return w


def _clips(*specs):
    # spec: (asset, screen, version)
    return [f"/drop/PRJ001_D01_S01_A{a:03d}_{s}_ANIM_V{v:03d}.mp4" for a, s, v in specs]


def test_ten_clips_make_five_two_screen_jobs(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    specs = []
    for a in range(1, 6):
        specs += [(a, "LEFT", 1), (a, "RIGHT", 1)]
    w._on_watch_clips_ready(_clips(*specs))

    assert len(w._jobs) == 5, "expected one previz job per asset"
    for j in w._jobs:
        assert len(j.material_assignments) == 2
        mats = {a.material_name for a in j.material_assignments}
        assert mats == {"LEFT", "RIGHT"}
        assert j.scene_path.endswith("Stage.blend")
        assert "_PREVIZ_V001" in j.output_path


def test_newer_version_updates_job_in_place(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    w._on_watch_clips_ready(_clips((1, "LEFT", 1), (1, "RIGHT", 1)))
    assert len(w._jobs) == 1
    first_id = w._jobs[0].id

    # a newer LEFT version lands → same job updated, not a second job
    w._on_watch_clips_ready(_clips((1, "LEFT", 3), (1, "RIGHT", 1)))
    assert len(w._jobs) == 1
    assert w._jobs[0].id == first_id
    left = next(a for a in w._jobs[0].material_assignments if a.material_name == "LEFT")
    assert left.video_path.endswith("V003.mp4")
    assert "_PREVIZ_V003" in w._jobs[0].output_path


def test_same_version_is_idempotent(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    clips = _clips((2, "CENTER", 5))
    w._on_watch_clips_ready(clips)
    w._on_watch_clips_ready(clips)   # second poll, nothing new
    assert len(w._jobs) == 1


def test_screen_to_material_override(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    w._asset_grouping.screen_to_material = {"CENTER": "Center_Screen"}
    w._on_watch_clips_ready(_clips((7, "CENTER", 1)))
    mats = {a.material_name for a in w._jobs[0].material_assignments}
    assert mats == {"Center_Screen"}


def test_setup_to_scene_routes_to_its_scene(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    w._asset_grouping.setup_to_scene = {2: "/scenes/StageB.blend"}
    # S02 clip routes to StageB; S01 clip falls back to the current scene
    w._on_watch_clips_ready([
        "/drop/PRJ001_D01_S02_A001_LEFT_ANIM_V001.mp4",
        "/drop/PRJ001_D01_S01_A001_LEFT_ANIM_V001.mp4",
    ])
    scenes = {j.scene_path for j in w._jobs}
    assert "/scenes/StageB.blend" in scenes
    assert any(s.endswith("Stage.blend") for s in scenes)
