"""Window ↔ WatchPanel wiring (WatchMixin): load/apply round-trips, first-run
acknowledgement, profile persistence across a window rebuild, resilience of the
ingest path to a broken pattern, and the crash-report offer gating. These are
the seams the headless smoke test constructs but never exercises."""
from __future__ import annotations


def _make_window(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app_qt
    monkeypatch.setattr(app_qt, "PROFILE_PATH", tmp_path / "p.json")
    monkeypatch.setattr(app_qt, "HISTORY_PATH", tmp_path / "h.json")
    monkeypatch.setattr(app_qt, "LOG_PATH", tmp_path / "l.txt")
    monkeypatch.setattr(app_qt, "CRASH_DIR", tmp_path / "crashes")
    w = app_qt.BlenderVideoMapperQt()
    w._blender_path = ""
    w._check_updates_on_launch = False
    return w


def test_apply_watch_panel_writes_config_back(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    wp = w.watch_panel
    wp.previz_radio.setChecked(True)
    wp.pattern_edit.setText("{ID#}_D{Day#}_{Sec}_{Cue}_{Screen}_v{Ver#}")
    wp.content_edit.setText("")
    wp.screen_table.set_pairs({"TC-MASTER": "TC-MASTER", "DJBS": "DJBS"})
    wp.setup_table.set_pairs({1: "/x/D1.c4d", 2: "/x/D2.c4d"})
    wp.deliver_edit.setText("/deliver")
    w._apply_watch_panel()
    ag = w._asset_grouping
    assert ag.enabled is True
    assert ag.pattern == "{ID#}_D{Day#}_{Sec}_{Cue}_{Screen}_v{Ver#}"
    assert ag.content_type == ""
    assert ag.screen_to_material == {"TC-MASTER": "TC-MASTER", "DJBS": "DJBS"}
    assert ag.setup_to_scene == {1: "/x/D1.c4d", 2: "/x/D2.c4d"}
    assert w._deliver_dir == "/deliver"


def test_load_watch_panel_mirrors_config(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    ag = w._asset_grouping
    ag.enabled = True
    ag.pattern = "{P}_S{Setup#}_{Screen}_V{Version#}"
    ag.screen_to_material = {"LEFT": "Wall_L"}
    ag.setup_to_scene = {3: "/scenes/three.c4d"}
    w._deliver_dir = "/out/deliver"
    w._load_watch_panel()
    wp = w.watch_panel
    assert wp.previz_radio.isChecked()
    assert wp.pattern_edit.text() == ag.pattern
    assert wp.screen_table.get_pairs() == {"LEFT": "Wall_L"}
    assert wp.setup_table.get_pairs() == {3: "/scenes/three.c4d"}
    assert wp.deliver_edit.text() == "/out/deliver"


def test_first_run_dismiss_persists_and_hides_banner(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    assert w._watch_first_run_seen is False
    w._on_watch_first_run_dismissed()
    assert w._watch_first_run_seen is True
    # A rebuilt window (fresh profile load) must not resurrect the banner.
    w2 = _make_window(tmp_path, monkeypatch)
    assert w2._watch_first_run_seen is True


def test_watch_settings_survive_window_rebuild(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    wp = w.watch_panel
    wp.previz_radio.setChecked(True)
    wp.pattern_edit.setText("{ID#}_D{Day#}_{Screen}_v{Ver#}")
    wp.screen_table.set_pairs({"TC-MASTER": "TC-MASTER"})
    wp.setup_table.set_pairs({2: "/x/D2.c4d"})
    wp.deliver_edit.setText("/deliver")
    w._apply_watch_panel()
    w._save_profile()

    w2 = _make_window(tmp_path, monkeypatch)
    ag2 = w2._asset_grouping
    assert ag2.enabled is True
    assert ag2.pattern == "{ID#}_D{Day#}_{Screen}_v{Ver#}"
    assert ag2.screen_to_material == {"TC-MASTER": "TC-MASTER"}
    assert ag2.setup_to_scene == {2: "/x/D2.c4d"}
    assert w2._deliver_dir == "/deliver"


def test_broken_pattern_never_crashes_ingest(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    w._asset_grouping.enabled = True
    w._asset_grouping.pattern = "(?P<bad"                    # unclosed group
    before = len(w._jobs)
    w._on_watch_clips_ready(["/drop/PRJ_D01_S01_A001_LEFT_ANIM_V001.mp4"])
    assert len(w._jobs) == before, "broken pattern must not build jobs"


def test_hyphenated_convention_builds_previz_job(tmp_path, monkeypatch):
    # The real-world convention end to end: friendly pattern with hyphenated
    # codes → group → previz job with a resolved output name.
    w = _make_window(tmp_path, monkeypatch)
    ag = w._asset_grouping
    ag.enabled = True
    ag.pattern = "{ID#}_D{Day#}_{Sec}_{Cue}_{Screen}_v{Ver#}"
    ag.content_type = ""
    ag.screen_to_material = {}
    ag.setup_to_scene = {}
    w.scene_panel.scene_edit.setText(str(tmp_path / "Stage.c4d"))
    w._on_watch_clips_ready(["/drop/80230_D2_War-Treaty_MusicH_TC-MASTER_v001.mp4"])
    assert len(w._jobs) == 1
    job = w._jobs[0]
    assert any(a.material_name == "TC-MASTER" for a in job.material_assignments)
    assert job.output_path and "{" not in job.output_path   # tokens resolved


def test_crash_offer_headless_noop_and_windowed_ack(tmp_path, monkeypatch):
    import app_qt
    from core import crash
    w = _make_window(tmp_path, monkeypatch)
    crash_dir = tmp_path / "crashes"
    report = crash.write_crash_report(crash_dir, "ValueError: boom", version="t")
    assert report is not None

    # Headless (offscreen): must be a silent no-op, report stays pending.
    w._offer_crash_reports()
    assert crash.pending_reports(crash_dir) == [report]

    # Windowed: the dialog is offered once, then everything is acknowledged.
    asked: dict[str, str] = {}
    monkeypatch.setattr(w, "_is_headless", lambda: False)
    monkeypatch.setattr(app_qt, "ask",
                        lambda *a, **k: asked.setdefault("title", a[1]) and "dismiss")
    w._offer_crash_reports()
    assert "crashed last time" in asked["title"]
    assert crash.pending_reports(crash_dir) == []


def test_recursive_toggle_scans_subfolders(tmp_path, monkeypatch):
    import time as _time

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    w = _make_window(tmp_path, monkeypatch)
    sp = w.scene_panel
    wf = tmp_path / "watch"
    (wf / "day2").mkdir(parents=True)
    (wf / "top_v1.mp4").write_bytes(b"x")
    (wf / "day2" / "deep_v1.mp4").write_bytes(b"x")
    out = wf / "PREVIZ"
    out.mkdir()
    (out / "rendered_v1.mp4").write_bytes(b"x")     # must NEVER be ingested
    sp.set_watch_ignore_dir(str(out))
    sp._watch_folder = str(wf)

    def _scan_paths():
        got: dict[str, list] = {}
        sp._watch_scanned.connect(lambda listing: got.setdefault("l", listing))
        sp._scan_watch_folder()
        for _ in range(100):
            app.processEvents()
            if "l" in got:
                break
            _time.sleep(0.02)
        return sorted(p for p, _s, _m, _d in got.get("l", []))

    flat = _scan_paths()
    assert any(p.endswith("top_v1.mp4") for p in flat)
    assert not any("day2" in p for p in flat), "non-recursive must stay top-level"

    sp.set_watch_recursive(True)
    deep = _scan_paths()
    assert any(p.endswith("deep_v1.mp4") for p in deep), "recursive must find subfolder clips"
    assert not any("PREVIZ" in p for p in deep), "output subtree must stay excluded"


def test_recursive_round_trips_through_panel_and_profile(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    w.watch_panel.recursive_check.setChecked(True)      # emits config_changed → apply
    assert w.scene_panel.get_watch_recursive() is True
    w._save_profile()
    w2 = _make_window(tmp_path, monkeypatch)
    assert w2.scene_panel.get_watch_recursive() is True
    assert w2.watch_panel.recursive_check.isChecked()


def test_ignored_clips_badge_counts_non_matching(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    ag = w._asset_grouping
    ag.enabled = True
    ag.pattern = "{ID#}_D{Day#}_{Sec}_{Cue}_{Screen}_v{Ver#}"
    ag.content_type = ""
    ag.screen_to_material = {}
    ag.setup_to_scene = {}
    w.watch_panel.set_mode(True)      # reflect previz without re-emitting config
    w.scene_panel.scene_edit.setText(str(tmp_path / "Stage.c4d"))
    w._on_watch_clips_ready([
        "/drop/80230_D2_War-Treaty_MusicH_TC-MASTER_v001.mp4",   # matches → job
        "/drop/nonsense-name.mp4",                               # no match → ignored
        "/drop/also wrong.mov",                                  # no match → ignored
    ])
    assert len(w._jobs) == 1
    assert w.watch_panel._ignored_count == 2
    assert w.watch_panel.ignored_btn.isVisible() or not w.watch_panel.isVisible()
    assert "2" in w.watch_panel.ignored_btn.text()


def test_activity_feed_persists_and_preloads(tmp_path, monkeypatch):
    w = _make_window(tmp_path, monkeypatch)
    w.watch_panel.add_activity("Previz", "2 new job(s) from 1 group(s)")
    hist = tmp_path / "l.txt"
    hist = hist.parent / "ingest_history.log"
    text = hist.read_text()
    assert "Previz" in text and "2 new job(s)" in text
    # A rebuilt window preloads the tail into the feed.
    w2 = _make_window(tmp_path, monkeypatch)
    rows = w2.watch_panel.activity.rowCount()
    assert rows >= 1
    assert w2.watch_panel.activity.item(0, 1).text() == "Previz"
    # Clearing the visible feed must keep the on-disk audit trail.
    w2.watch_panel.clear_activity()
    assert hist.exists() and "Previz" in hist.read_text()
