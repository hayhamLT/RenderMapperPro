import pytest

from core.models import JobConfig, MaterialVideoAssignment, RenderOptions
from core.runner import build_blender_command, submit_deadline_job


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


def test_c4d_deadline_requires_local_c4dpy_to_bake(tmp_path):
    """A .c4d Deadline job bakes the scene locally first; without c4dpy on the
    submitting machine it raises a clear error rather than submitting garbage."""
    repo = tmp_path / "repo"
    repo.mkdir()
    scene = tmp_path / "studio.c4d"
    scene.write_text("x")
    worker = tmp_path / "c4d_worker.py"
    worker.write_text("# worker")
    opts = RenderOptions(width=1920, height=1080, fps=30, frame_start=1, frame_end=10, engine="Redshift")
    job = JobConfig(scene_path=str(scene), video_path="", target_material="Video", target_camera="Cam",
                    output_path=str(tmp_path / "out"), render=opts, use_deadline=True,
                    material_assignments=[MaterialVideoAssignment("Video", "/v.mp4")])
    job.deadline_repo_path = str(repo)
    job.deadline_command_path = "/bin/echo"
    with pytest.raises(RuntimeError, match="c4dpy"):
        submit_deadline_job("blender", str(tmp_path / "bw.py"), job,
                            c4dpy_executable="", c4d_worker_script=str(worker))


def test_blender_deadline_still_uses_commandline(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    scene = tmp_path / "s.blend"
    scene.write_text("x")
    worker = tmp_path / "blender_worker.py"
    worker.write_text("# w")
    opts = RenderOptions(width=2, height=2, fps=30, frame_start=1, frame_end=2)
    job = JobConfig(scene_path=str(scene), video_path="", target_material="", target_camera="",
                    output_path=str(tmp_path / "o"), render=opts, use_deadline=True)
    job.deadline_repo_path = str(repo)
    job.deadline_command_path = "/bin/echo"
    rc = submit_deadline_job("/b/blender", str(worker), job)
    assert rc == 0
    staged = next((repo / "custom" / "blender_jobs").iterdir())
    assert "Plugin=CommandLine" in (staged / "job_info.job").read_text()
    plugin_info = (staged / "plugin_info.job").read_text()
    assert "Executable=/b/blender" in plugin_info
    # Per-task frame range so Deadline can chunk instead of every task rendering all.
    assert "<STARTFRAME> <ENDFRAME>" in plugin_info
