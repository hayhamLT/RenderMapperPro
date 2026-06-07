from core.models import JobConfig, MaterialVideoAssignment, RenderOptions


def _opts() -> RenderOptions:
    return RenderOptions(width=1920, height=1080, fps=24, frame_start=1, frame_end=250)


def _job(**kw) -> JobConfig:
    return JobConfig("s.blend", "v.mp4", "Mat", "Cam", "/out", _opts(), **kw)


def test_render_options_defaults():
    o = _opts()
    assert o.engine == "CYCLES"
    assert o.resolution_percentage == 100
    assert o.use_denoise is True


def test_preview_frame_defaults_to_zero():
    assert _job().preview_frame == 0
    assert _job().to_json_dict()["preview_frame"] == 0


def test_preview_frame_roundtrips():
    assert _job(preview_frame=120).to_json_dict()["preview_frame"] == 120


def test_engine_uppercased_in_json():
    job = JobConfig("s.blend", "v.mp4", "Mat", "Cam", "/out",
                    RenderOptions(width=1, height=1, fps=24, frame_start=1, frame_end=1, engine="cycles"))
    assert job.to_json_dict()["render"]["engine"] == "CYCLES"


def test_material_assignments_fallback_from_target():
    d = _job().to_json_dict()
    assert d["material_assignments"] == [
        {"material_name": "Mat", "video_path": "v.mp4", "mapping_mode": "EMISSION_FULL_BRIGHT"}
    ]


def test_explicit_assignments_preserved():
    job = _job()
    job.material_assignments = [MaterialVideoAssignment("Screen", "clip.mov", "BASE_COLOR_ALPHA")]
    d = job.to_json_dict()
    assert d["material_assignments"][0]["material_name"] == "Screen"
    assert d["material_assignments"][0]["mapping_mode"] == "BASE_COLOR_ALPHA"
