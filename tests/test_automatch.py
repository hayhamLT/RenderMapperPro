from core.utils import auto_match_media_to_materials, match_score, normalize_match_name


def test_normalize_strips_affixes_and_dup_suffix():
    assert normalize_match_name("M_Screen_01") == "screen 01"
    assert normalize_match_name("Screen.001") == "screen"
    assert normalize_match_name("Wall_MAT") == "wall"


def test_filename_contains_material():
    mats = ["Screen", "Wall", "Floor"]
    files = ["/v/Screen_final_v3.mp4", "/v/wall.mov", "/v/unrelated_clip.mp4"]
    m = auto_match_media_to_materials(mats, files)
    assert m["Screen"].endswith("Screen_final_v3.mp4")
    assert m["Wall"].endswith("wall.mov")
    assert "Floor" not in m                      # nothing contains 'floor' → left manual


def test_one_to_one_no_double_assignment():
    # Both materials could match the same file; each file used once.
    mats = ["Screen", "Screen_01"]
    files = ["/v/Screen_01.mp4", "/v/Screen.mp4"]
    m = auto_match_media_to_materials(mats, files)
    assert m["Screen_01"].endswith("Screen_01.mp4")   # exact wins
    assert m["Screen"].endswith("Screen.mp4")
    assert len(set(m.values())) == len(m)


def test_short_or_unrelated_names_not_forced():
    assert match_score("M", "anything.mp4") == 0.0          # too short
    assert match_score("Video", "ALL_REEL_2025.mp4") == 0.0  # 'video' not present
    m = auto_match_media_to_materials(["Video"], ["/v/ALL_REEL_2025.mp4"])
    assert m == {}


def test_normalized_separators_match():
    m = auto_match_media_to_materials(["TV Screen 01"], ["/v/tv-screen-01_comp.mp4"])
    assert m["TV Screen 01"].endswith("tv-screen-01_comp.mp4")
