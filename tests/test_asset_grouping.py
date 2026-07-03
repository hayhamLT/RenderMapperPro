from core.asset_grouping import (
    DEFAULT_OUTPUT_TEMPLATE,
    AssetGroup,
    GroupingConfig,
    group_clips,
    parse_clip,
)


def test_parse_canonical_name():
    c = parse_clip("/x/PRJ001_D01_S01_A017_CENTER_ANIM_V003.mp4")
    assert c is not None
    assert (c.prj, c.day, c.setup, c.asset) == ("PRJ001", 1, 1, 17)
    assert c.screen == "CENTER"
    assert c.type == "ANIM"
    assert c.version == 3
    assert c.asset_key == ("PRJ001", 1, 17)


def test_parse_non_conforming_is_skipped():
    assert parse_clip("/x/random_clip.mp4") is None
    assert parse_clip("/x/PRJ001_D01_S01.mp4") is None


def test_group_two_screens_of_one_asset():
    paths = [
        "/x/PRJ001_D01_S01_A017_LEFT_ANIM_V003.mp4",
        "/x/PRJ001_D01_S01_A017_CENTER_ANIM_V003.mp4",
        "/x/PRJ001_D01_S01_A017_RIGHT_ANIM_V003.mp4",
    ]
    groups = group_clips(paths, GroupingConfig())
    assert len(groups) == 1
    g = groups[0]
    assert g.asset == 17 and g.setup == 1
    assert set(g.screens) == {"LEFT", "CENTER", "RIGHT"}


def test_latest_version_per_screen_wins():
    paths = [
        "/x/PRJ001_D01_S01_A017_CENTER_ANIM_V003.mp4",
        "/x/PRJ001_D01_S01_A017_CENTER_ANIM_V011.mp4",  # newer
        "/x/PRJ001_D01_S01_A017_CENTER_ANIM_V009.mp4",
    ]
    g = group_clips(paths, GroupingConfig())[0]
    assert g.screens["CENTER"].endswith("V011.mp4")
    assert g.version == 11


def test_content_type_filter_excludes_stills():
    paths = [
        "/x/PRJ001_D01_S01_A017_CENTER_ANIM_V003.mp4",
        "/x/PRJ001_D01_S01_A017_CENTER_STILL_V003.png",  # filtered out
    ]
    g = group_clips(paths, GroupingConfig())[0]
    assert g.screens["CENTER"].endswith("ANIM_V003.mp4")
    # disabling the filter keeps whichever version is newest regardless of type
    paths2 = ["/x/PRJ001_D01_S01_A018_LEFT_STILL_V005.png"]
    assert group_clips(paths2, GroupingConfig(content_type="")) != []
    assert group_clips(paths2, GroupingConfig(content_type="ANIM")) == []


def test_distinct_assets_and_setups_make_distinct_groups():
    paths = [
        "/x/PRJ001_D01_S01_A017_CENTER_ANIM_V001.mp4",
        "/x/PRJ001_D01_S01_A018_CENTER_ANIM_V001.mp4",   # different asset
        "/x/PRJ001_D01_S02_A017_CENTER_ANIM_V001.mp4",   # different setup
    ]
    groups = group_clips(paths, GroupingConfig())
    keys = {(g.setup, g.asset) for g in groups}
    assert keys == {(1, 17), (1, 18), (2, 17)}
    # ordered by (setup, asset)
    assert [(g.setup, g.asset) for g in groups] == [(1, 17), (1, 18), (2, 17)]


def test_ten_clips_five_two_screen_assets():
    paths = []
    for n in range(1, 6):
        paths.append(f"/x/PRJ001_D01_S01_A{n:03d}_LEFT_ANIM_V001.mp4")
        paths.append(f"/x/PRJ001_D01_S01_A{n:03d}_RIGHT_ANIM_V001.mp4")
    groups = group_clips(paths, GroupingConfig())
    assert len(groups) == 5
    assert all(set(g.screens) == {"LEFT", "RIGHT"} for g in groups)


def test_output_name_zero_padded():
    g = AssetGroup(prj="PRJ001", day=1, setup=1, asset=17,
                   screens={"CENTER": "/x/c.mp4"}, version=3)
    assert g.output_name(DEFAULT_OUTPUT_TEMPLATE) == "PRJ001_D01_S01_A017_PREVIZ_V003"
    # custom template with pv + cam tokens, cam left blank
    name = g.output_name("{prj}_S{setup}_PV{pv}_V{ver}")
    assert name == "PRJ001_S01_PV017_V003"


def test_output_name_unknown_token_left_intact():
    g = AssetGroup(prj="P", day=1, setup=1, asset=1, screens={}, version=1)
    assert "{bogus}" in g.output_name("{prj}_{bogus}_V{ver}")


def test_material_assignments_use_override_then_fallback():
    g = AssetGroup(prj="P", day=1, setup=1, asset=1,
                   screens={"CENTER": "/c.mp4", "LEFT": "/l.mp4"}, version=1)
    pairs = g.material_assignments({"CENTER": "Center_Screen"})
    # sorted by screen code: CENTER (overridden) then LEFT (fallback to code)
    assert pairs == [("Center_Screen", "/c.mp4"), ("LEFT", "/l.mp4")]


def test_config_roundtrip():
    cfg = GroupingConfig(enabled=True, content_type="ANIM",
                         screen_to_material={"CENTER": "Center_Screen"},
                         setup_to_scene={1: "/scenes/StageA.blend", 2: "/scenes/StageB.blend"})
    back = GroupingConfig.from_dict(cfg.to_dict())
    assert back.enabled is True
    assert back.screen_to_material == {"CENTER": "Center_Screen"}
    assert back.setup_to_scene == {1: "/scenes/StageA.blend", 2: "/scenes/StageB.blend"}


def test_case_insensitive_fixed_letters():
    c = parse_clip("/x/prj001_d01_s01_a017_center_anim_v003.mp4")
    assert c is not None and c.asset == 17 and c.screen == "center"


def test_default_pattern_is_now_the_friendly_token_form():
    # The default is the readable token pattern, not a raw regex.
    from core.asset_grouping import DEFAULT_PATTERN
    assert "{" in DEFAULT_PATTERN and "regex" not in DEFAULT_PATTERN.lower()
    assert "{Project}" in DEFAULT_PATTERN and "{Version#}" in DEFAULT_PATTERN


def test_custom_friendly_pattern_with_human_field_names():
    # A per-show pattern written in the friendly syntax; Proj/Ver alias to prj/version.
    cfg = GroupingConfig(pattern="{Proj}-{Screen}-S{Setup#}-A{Asset#}-D{Day#}-V{Ver#}")
    c = parse_clip("/x/ACME-LEFT-S2-A7-D3-V5.mov", cfg.pattern)
    assert c is not None
    assert (c.prj, c.screen, c.setup, c.asset, c.day, c.version) == ("ACME", "LEFT", 2, 7, 3, 5)


def test_legacy_regex_pattern_still_supported_for_saved_configs():
    # Anyone who saved the old raw regex keeps working untouched.
    legacy = (
        r"^(?P<prj>[A-Za-z][A-Za-z0-9]*?)_[Dd](?P<day>\d+)_[Ss](?P<setup>\d+)"
        r"_[Aa](?P<asset>\d+)_(?P<screen>[A-Za-z0-9]+)_(?P<type>[A-Za-z0-9]+)_[Vv](?P<version>\d+)$"
    )
    c = parse_clip("/x/PRJ001_D01_S01_A017_CENTER_ANIM_V003.mp4", legacy)
    assert c is not None and c.asset == 17 and c.version == 3
    # And it groups identically to the friendly default.
    paths = ["/x/PRJ001_D01_S01_A017_LEFT_ANIM_V001.mp4",
             "/x/PRJ001_D01_S01_A017_RIGHT_ANIM_V001.mp4"]
    assert len(group_clips(paths, GroupingConfig(pattern=legacy))) == 1


def test_invalid_friendly_pattern_skips_clip_gracefully():
    assert parse_clip("/x/whatever.mp4", "{Day#}_{Day#}") is None   # duplicate field
