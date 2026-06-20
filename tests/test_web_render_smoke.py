"""End-to-end smoke for the headless three.js backend: render one real frame.

Exercises the whole web path through the production entry point (run_web_job) —
Chromium launch, the vendored three.js (no CDN), GLB parse, GL render, PNG
capture, file write. This is the regression guard that the vendored renderer
keeps working offline.

Skips where Chromium isn't installed (the ordinary lint-test gate has no
browser); the dedicated CI web-smoke job runs `playwright install chromium`
first, so it runs there. The test scene 3DScene/tv studio.glb ships in-repo.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import web_render
from core.models import JobConfig, RenderOptions

pytest.importorskip("playwright")

ROOT = Path(__file__).resolve().parent.parent
SCENE = ROOT / "3DScene" / "tv studio.glb"


@pytest.mark.skipif(not SCENE.exists(), reason="test scene 3DScene/tv studio.glb missing")
@pytest.mark.skipif(not web_render.web_chromium_installed(), reason="Playwright Chromium not installed")
def test_web_render_one_frame(tmp_path: Path) -> None:
    logs: list[str] = []
    opts = RenderOptions(width=160, height=120, fps=24, frame_start=1, frame_end=1)
    # preview_frame=1 → render exactly one frame to a PNG (no ffmpeg/movie step).
    job = JobConfig(str(SCENE), "", "", "", str(tmp_path), opts, preview_frame=1)

    rc = web_render.run_web_job(job, on_log=lambda *a: logs.append(" ".join(str(x) for x in a)))

    assert rc == 0, f"web render returned {rc}; last logs: {logs[-6:]}"
    out = tmp_path / "preview_00001.png"
    assert out.exists(), f"no preview frame written; last logs: {logs[-6:]}"
    # A real rendered frame is well over a KB; a blank/failed capture is tiny.
    assert out.stat().st_size > 1000, f"preview frame suspiciously small ({out.stat().st_size} bytes)"
