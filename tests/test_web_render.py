"""Tests for the web (three.js) backend's pure dispatch/mapping logic.
(The render itself needs Playwright + Chromium and is verified separately via
prototypes/web_render/verify_backend.py.)"""
from core.models import JobConfig, MaterialVideoAssignment, RenderOptions
from core.web_render import _clip_mappings, is_web_scene


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
