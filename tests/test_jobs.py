"""Tests for the extracted job-level domain logic."""
from core.jobs import disk_space_warnings, estimate_job_bytes, migrate_profile
from core.models import RenderJob, RenderOptions


def _job(out: str = "") -> RenderJob:
    opts = RenderOptions(width=1920, height=1080, fps=24, frame_start=1, frame_end=10)
    return RenderJob(id=1, output_path=out, render_options=opts)


def test_estimate_job_bytes_positive():
    assert estimate_job_bytes(_job()) > 0


def test_estimate_job_bytes_no_options():
    assert estimate_job_bytes(RenderJob(id=1)) == 0


def test_disk_space_warnings_skips_empty_paths():
    assert disk_space_warnings([_job(out="")]) == []


def test_migrate_profile_bumps_version():
    out = migrate_profile({"version": 1, "x": 1}, 3)
    assert out["version"] == 3
    assert out["x"] == 1


def test_migrate_profile_same_version_returns_input():
    d = {"version": 3}
    assert migrate_profile(d, 3) is d


def test_migrate_profile_newer_loaded_asis_and_logs():
    logs: list[str] = []
    d = {"version": 99}
    out = migrate_profile(d, 3, logs.append)
    assert out is d
    assert any("newer" in m for m in logs)
