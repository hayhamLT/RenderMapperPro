"""Proof-of-concept: a headless-browser (three.js) render backend.

Mirrors the app's real pipeline end to end:
  clip --ffmpeg--> PNG frames --three.js (headless Chrome)--> rendered frames
  --ffmpeg--> mp4

This is a standalone spike (it does NOT touch app_qt / the Blender / C4D paths).
It proves the novel/risky part — driving a headless three.js renderer frame by
frame and capturing deterministic output — before committing to a full backend.

Run:  .venv/bin/python prototypes/web_render/render_poc.py
"""
from __future__ import annotations

import base64
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]


def _ffmpeg() -> str:
    mach = platform.machine().lower()
    arch = "arm64" if ("arm" in mach or "aarch64" in mach) else "x64"
    sysn = "darwin" if sys.platform == "darwin" else ("win32" if sys.platform.startswith("win") else "linux")
    exe = "ffmpeg.exe" if sysn == "win32" else "ffmpeg"
    p = REPO / "vendor" / "ffmpeg" / f"{sysn}-{arch}" / exe
    return str(p) if p.exists() else "ffmpeg"


def main() -> int:
    ff = _ffmpeg()
    work = Path(tempfile.mkdtemp(prefix="webrender_poc_"))
    clip = work / "clip"
    clip.mkdir()
    out = work / "out"
    out.mkdir()

    # 1) Make a test clip and extract it to PNG frames (the app already does this
    #    ffmpeg frame extraction for the C4D path — we reuse the same idea).
    subprocess.run(
        [ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x180:rate=24",
         str(clip / "f_%04d.png")],
        check=True, capture_output=True)
    frames = sorted(clip.glob("*.png"))
    print(f"clip frames extracted: {len(frames)}")

    # 2) Render each frame through headless three.js.
    from playwright.sync_api import sync_playwright
    backend = "?"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            # Guarantee a software fallback so the spike runs on GPU-less machines;
            # on a real GPU workstation you'd drop these and get hardware accel.
            "--enable-unsafe-swiftshader",
            "--use-angle=swiftshader",
        ])
        page = browser.new_page(viewport={"width": 720, "height": 420})
        page.goto((ROOT / "scene.html").as_uri())
        page.wait_for_function("window.__ready === true || window.__error !== ''", timeout=45000)
        err = page.evaluate("window.__error")
        if err:
            print(f"ERROR: scene setup failed:\n{err}", file=sys.stderr)
            browser.close()
            return 1
        backend = page.evaluate("window.__backend")
        canvas = page.query_selector("canvas")
        assert canvas is not None, "no canvas in page"
        for i, fp in enumerate(frames):
            data = base64.b64encode(fp.read_bytes()).decode("ascii")
            page.evaluate("([d, a]) => window.renderFrame(d, a)",
                          [f"data:image/png;base64,{data}", i * 3])
            canvas.screenshot(path=str(out / f"out_{i:04d}.png"))
        browser.close()
    rendered = sorted(out.glob("*.png"))
    print(f"backend used: {backend}")
    print(f"frames rendered: {len(rendered)}")

    # 3) Assemble the rendered frames into an mp4 (the app's existing final step).
    mp4 = work / "web_render_poc.mp4"
    subprocess.run(
        [ff, "-y", "-framerate", "24", "-i", str(out / "out_%04d.png"),
         "-pix_fmt", "yuv420p", str(mp4)],
        check=True, capture_output=True)
    ok = mp4.exists() and mp4.stat().st_size > 0
    print(f"mp4: {mp4}  ({mp4.stat().st_size if ok else 0} bytes)")
    print("RESULT:", "OK" if (ok and len(rendered) == len(frames)) else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
