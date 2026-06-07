from core.runner import build_blender_command


def test_blend_scene_is_passed_positionally():
    cmd = build_blender_command("/b/blender", "/x/Scene.blend", "/w/worker.py", "/c/cfg.json")
    assert cmd[:2] == ["/b/blender", "-b"]
    assert "/x/Scene.blend" in cmd
    assert cmd[-4:] == ["--python", "/w/worker.py", "--", "/c/cfg.json"]


def test_non_blend_scene_is_imported_by_worker_not_passed():
    cmd = build_blender_command("/b/blender", "/x/Scene.fbx", "/w/worker.py", "/c/cfg.json")
    assert "/x/Scene.fbx" not in cmd
    assert cmd[-4:] == ["--python", "/w/worker.py", "--", "/c/cfg.json"]


def test_blend_extension_case_insensitive():
    cmd = build_blender_command("/b/blender", "/x/Scene.BLEND", "/w/worker.py", "/c/cfg.json")
    assert "/x/Scene.BLEND" in cmd
