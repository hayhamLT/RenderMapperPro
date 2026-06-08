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


def test_c4d_deadline_submission_uses_rendermapperpro_plugin(tmp_path):
    """A .c4d Deadline job is submitted with the RenderMapperPro plugin, staging
    the scene/video/worker/config and passing repo-relative paths for the node
    plugin to re-map."""
    import json
    from core.models import JobConfig, RenderOptions, MaterialVideoAssignment
    from core.runner import submit_deadline_job

    repo = tmp_path / "repo"; repo.mkdir()
    src = tmp_path / "src"; src.mkdir()
    scene = src / "studio.c4d"; scene.write_text("x")
    vid = src / "reel.mp4"; vid.write_text("x")
    worker = src / "c4d_worker.py"; worker.write_text("# worker")

    opts = RenderOptions(width=1920, height=1080, fps=30, frame_start=1, frame_end=10,
                         engine="Redshift", output_format="MPEG4")
    job = JobConfig(scene_path=str(scene), video_path=str(vid), target_material="Video",
                    target_camera="Cam", output_path=str(src / "out.mp4"), render=opts,
                    use_deadline=True, ffmpeg_path="/some/ffmpeg",
                    material_assignments=[MaterialVideoAssignment("Video", str(vid))])
    job.deadline_repo_path = str(repo)
    job.deadline_command_path = "/bin/echo"

    rc = submit_deadline_job("blender", str(src / "blender_worker.py"), job,
                             c4d_worker_script=str(worker))
    assert rc == 0
    staged = next((repo / "custom" / "blender_jobs").iterdir())
    job_info = (staged / "job_info.job").read_text()
    assert "Plugin=RenderMapperPro" in job_info
    plugin = (staged / "plugin_info.job").read_text()
    assert f"SubmitRepoRoot={repo}" in plugin
    assert "ConfigFile=custom/blender_jobs/" in plugin
    assert "WorkerScript=custom/blender_jobs/" in plugin and "c4d_worker.py" in plugin
    cfg = json.loads((staged / "blender_job_config.json").read_text())
    assert cfg["ffmpeg_path"] == ""                      # node uses PATH ffmpeg
    assert cfg["scene_path"].startswith(str(staged))     # scene staged into repo


def test_blender_deadline_still_uses_commandline(tmp_path):
    from core.models import JobConfig, RenderOptions
    from core.runner import submit_deadline_job
    repo = tmp_path / "repo"; repo.mkdir()
    scene = tmp_path / "s.blend"; scene.write_text("x")
    worker = tmp_path / "blender_worker.py"; worker.write_text("# w")
    opts = RenderOptions(width=2, height=2, fps=30, frame_start=1, frame_end=2)
    job = JobConfig(scene_path=str(scene), video_path="", target_material="", target_camera="",
                    output_path=str(tmp_path / "o"), render=opts, use_deadline=True)
    job.deadline_repo_path = str(repo)
    job.deadline_command_path = "/bin/echo"
    rc = submit_deadline_job("/b/blender", str(worker), job)
    assert rc == 0
    staged = next((repo / "custom" / "blender_jobs").iterdir())
    assert "Plugin=CommandLine" in (staged / "job_info.job").read_text()
    assert "Executable=/b/blender" in (staged / "plugin_info.job").read_text()
