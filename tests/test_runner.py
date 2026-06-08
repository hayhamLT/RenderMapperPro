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


def test_c4d_deadline_submission_builds_c4dpy_launcher(tmp_path):
    """A .c4d Deadline job stages a launcher that pipes the c4dpy license and
    runs the C4D worker, with all paths rewritten into the repo staging dir."""
    import json
    from core.models import JobConfig, RenderOptions, MaterialVideoAssignment
    from core.runner import submit_deadline_job

    repo = tmp_path / "repo"; repo.mkdir()
    src = tmp_path / "src"; src.mkdir()
    scene = src / "studio.c4d"; scene.write_text("x")
    vid = src / "reel.mp4"; vid.write_text("x")
    ff = src / "ffmpeg"; ff.write_text("x")
    worker = src / "c4d_worker.py"; worker.write_text("# worker")

    opts = RenderOptions(width=1920, height=1080, fps=30, frame_start=1, frame_end=10,
                         engine="Redshift", output_format="MPEG4")
    job = JobConfig(scene_path=str(scene), video_path=str(vid), target_material="Video",
                    target_camera="Cam", output_path=str(src / "out.mp4"), render=opts,
                    use_deadline=True, ffmpeg_path=str(ff),
                    material_assignments=[MaterialVideoAssignment("Video", str(vid))])
    job.deadline_repo_path = str(repo)
    job.deadline_command_path = "/bin/echo"

    rc = submit_deadline_job("blender", str(src / "blender_worker.py"), job,
                             c4dpy_executable="/opt/c4dpy", c4d_worker_script=str(worker))
    assert rc == 0
    staged = next((repo / "custom" / "blender_jobs").iterdir())
    plugin = (staged / "plugin_info.job").read_text()
    assert "c4d_launch.sh" in plugin and "<STARTFRAME> <ENDFRAME>" in plugin
    launcher = (staged / "c4d_launch.sh").read_text()
    assert "/opt/c4dpy" in launcher and "printf '1" in launcher
    cfg = json.loads((staged / "blender_job_config.json").read_text())
    assert cfg["ffmpeg_path"].startswith(str(staged))
    assert cfg["scene_path"].startswith(str(staged))


def test_c4d_deadline_requires_c4dpy(tmp_path):
    from core.models import JobConfig, RenderOptions
    from core.runner import submit_deadline_job
    scene = tmp_path / "s.c4d"; scene.write_text("x")
    worker = tmp_path / "c4d_worker.py"; worker.write_text("# w")
    opts = RenderOptions(width=1, height=1, fps=30, frame_start=1, frame_end=1)
    job = JobConfig(scene_path=str(scene), video_path="", target_material="", target_camera="",
                    output_path=str(tmp_path / "o"), render=opts, use_deadline=True)
    try:
        submit_deadline_job("blender", str(tmp_path / "bw.py"), job, c4d_worker_script=str(worker))
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "c4dpy" in str(e)
