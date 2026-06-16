"""Tests for the web (three.js) backend's pure dispatch/mapping logic.
(The render itself needs Playwright + Chromium and is verified separately via
prototypes/web_render/verify_backend.py.)"""
from core.models import JobConfig, MaterialVideoAssignment, RenderOptions
from core.web_render import _clip_frame_index, _clip_mappings, _is_software_renderer, is_web_scene


def _opts():
    return RenderOptions(width=8, height=8, fps=24, frame_start=1, frame_end=2)


def test_is_web_scene():
    assert is_web_scene("scene.glb")
    assert is_web_scene("/path/to/Scene.GLTF")
    assert not is_web_scene("scene.blend")
    assert not is_web_scene("scene.c4d")
    assert not is_web_scene("scene.fbx")


def test_clip_mappings_from_assignments():
    job = JobConfig("s.glb", "", "", "", "out.mp4", _opts(),
                    material_assignments=[
                        MaterialVideoAssignment("Screen", "/v/a.mp4"),
                        MaterialVideoAssignment("Wall", "/v/b.mp4")])
    assert _clip_mappings(job) == [("Screen", "/v/a.mp4"), ("Wall", "/v/b.mp4")]


def test_clip_mappings_fallback_to_target():
    job = JobConfig("s.glb", "/v/a.mp4", "Screen", "", "out.mp4", _opts())
    assert _clip_mappings(job) == [("Screen", "/v/a.mp4")]


def test_clip_mappings_empty():
    job = JobConfig("s.glb", "", "", "", "out.mp4", _opts())
    assert _clip_mappings(job) == []


def test_clip_frame_index_clamps():
    # 24-frame clip, timeline starts at frame 1, no offset.
    assert _clip_frame_index(1, 1, 0, 24) == 0       # first frame
    assert _clip_frame_index(12, 1, 0, 24) == 11     # mid
    assert _clip_frame_index(24, 1, 0, 24) == 23     # last
    assert _clip_frame_index(100, 1, 0, 24) == 23    # past end → hold last
    assert _clip_frame_index(-5, 1, 0, 24) == 0      # before start → hold first
    # preview offset (extraction started at the preview frame).
    assert _clip_frame_index(1050, 1, 1049, 1) == 0  # single-frame preview


def test_is_software_renderer():
    # Real GPU strings → not software (use the GPU launch).
    assert not _is_software_renderer("ANGLE (Apple, ANGLE Metal Renderer: Apple M4 Max)")
    assert not _is_software_renderer("Apple M1")
    # SwiftShader / software / empty → software (trigger the fallback).
    assert _is_software_renderer("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device))")
    assert _is_software_renderer("Google SwiftShader")
    assert _is_software_renderer("llvmpipe (software)")
    assert _is_software_renderer("")


def test_web_video_args_honours_codec_and_quality():
    """The three.js encode maps the job's codec + quality to ffmpeg args, instead
    of using ffmpeg defaults (which ignored the user's Output Format / quality)."""
    from core.web_render import _web_video_args

    def opts(**kw):
        return RenderOptions(width=8, height=8, fps=24, frame_start=1, frame_end=2, **kw)

    hi = _web_video_args(opts(video_quality="HIGH"))
    assert "libx264" in hi and hi[hi.index("-crf") + 1] == "18"
    lo = _web_video_args(opts(video_quality="LOW"))
    assert lo[lo.index("-crf") + 1] == "28"
    assert "libx265" in _web_video_args(opts(video_codec="H265"))
    assert "prores_ks" in _web_video_args(opts(video_codec="ProRes"))
