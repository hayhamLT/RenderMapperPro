"""Web render backend — headless three.js via Playwright + Chrome.

A third backend (alongside Blender and Cinema 4D / Redshift) for web-native
scenes (``.glb`` / ``.gltf``). Runs in-process: drives a headless Chromium that
loads ``assets/web_scene.html`` (three.js ``WebGPURenderer``, auto WebGL2
fallback), maps clips onto material emissive maps by name, renders frame by
frame, and assembles the result with the bundled ffmpeg.

Playwright is an optional dependency — imported lazily so the rest of the app
runs without it; a missing install yields a clear, actionable error.
"""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from .models import JobConfig
from .utils import subprocess_creation_flags

LogCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]

WEB_SCENE_EXTS = (".glb", ".gltf")

# --enable-unsafe-swiftshader: allow Chrome's software WebGL fallback so the
#   backend renders on GPU-less/headless machines (a real GPU is used when present).
# --allow-file-access-from-files: let the file:// scene page fetch the local
#   .glb/.gltf and its external textures/buffers (blocked by default).
_CHROME_ARGS = ["--enable-unsafe-swiftshader", "--allow-file-access-from-files"]

_PLAYWRIGHT_HINT = (
    "The web (three.js) backend needs Playwright. Install it with:\n"
    "    pip install playwright\n"
    "    python -m playwright install chromium"
)


def is_web_scene(scene_path: str) -> bool:
    return str(scene_path).lower().endswith(WEB_SCENE_EXTS)


def _resolve_scene_html() -> Path:
    """Locate the bundled web_scene.html (source tree or a frozen bundle)."""
    roots = [Path(__file__).resolve().parent.parent]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(getattr(sys, "_MEIPASS", "")))
    for root in roots:
        p = root / "assets" / "web_scene.html"
        if p.exists():
            return p
    raise FileNotFoundError("web_scene.html not found in assets/")


def _find_ffmpeg() -> str:
    try:
        from media import find_ffmpeg_tool
        f = find_ffmpeg_tool("ffmpeg")
        if f:
            return f
    except Exception:
        pass
    return "ffmpeg"


def _load_args(glb: Path) -> list:
    """(base64 bytes, is_binary) for handing a glb/gltf to the page's parser."""
    return [base64.b64encode(glb.read_bytes()).decode("ascii"), glb.suffix.lower() == ".glb"]


def _launch(pw, viewport_w: int, viewport_h: int):
    browser = pw.chromium.launch(headless=True, args=_CHROME_ARGS)
    page = browser.new_page(viewport={"width": viewport_w, "height": viewport_h})
    page.goto(_resolve_scene_html().as_uri())
    page.wait_for_function("window.__ready === true || window.__error !== ''", timeout=60000)
    err = page.evaluate("window.__error")
    if err:
        browser.close()
        raise RuntimeError(f"web scene page failed to initialize: {err}")
    return browser, page


def discover_web_scene(scene_path, on_log: LogCallback | None = None):
    """Scan a .glb/.gltf for material + camera names (mirrors the Blender/C4D
    discovery). Returns (materials, cameras, settings)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(_PLAYWRIGHT_HINT) from exc
    glb = Path(scene_path).expanduser().resolve()
    if not glb.exists():
        raise FileNotFoundError(f"Scene file not found: {glb}")
    if on_log:
        on_log(f"[web] Scanning {glb.name} via headless three.js…")
    with sync_playwright() as pw:
        browser, page = _launch(pw, 96, 96)
        try:
            page.evaluate("([w, h]) => window.api.init(w, h)", [64, 64])
            info = page.evaluate("([b, bin]) => window.api.loadGLB(b, bin)", _load_args(glb))
        finally:
            browser.close()
    return list(info.get("materials", [])), list(info.get("cameras", [])), {}


def _clip_mappings(job: JobConfig) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for a in (job.material_assignments or []):
        mn = getattr(a, "material_name", "")
        vp = getattr(a, "video_path", "")
        if mn and vp:
            out.append((mn, vp))
    if not out and job.target_material and job.video_path:
        out.append((job.target_material, job.video_path))
    return out


def run_web_job(job: JobConfig, on_log: LogCallback | None = None,
                should_cancel: CancelCheck | None = None) -> int:
    """Render a .glb/.gltf job with headless three.js. Returns 0 on success."""
    log = on_log or (lambda *_: None)
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RuntimeError(_PLAYWRIGHT_HINT) from exc

    glb = Path(job.scene_path).expanduser().resolve()
    if not glb.exists():
        raise FileNotFoundError(f"Scene file not found: {glb}")

    ff = _find_ffmpeg()
    r = job.render
    width, height = int(r.width), int(r.height)
    fs, fe = int(r.frame_start), int(r.frame_end)
    fps = int(r.fps) or 24
    total = max(1, fe - fs + 1)
    mappings = _clip_mappings(job)

    work = Path(tempfile.mkdtemp(prefix="webrender_"))
    out_dir = work / "out"
    out_dir.mkdir()

    # 1) Extract each unique clip to PNG frames (same idea as the C4D path).
    clip_frames: dict[str, list[Path]] = {}
    for _, vp in mappings:
        if vp in clip_frames:
            continue
        cdir = work / f"clip_{len(clip_frames)}"
        cdir.mkdir()
        log(f"[web] Extracting frames: {Path(vp).name}")
        subprocess.run([ff, "-y", "-i", vp, str(cdir / "f_%05d.png")],
                       check=True, capture_output=True,
                       creationflags=subprocess_creation_flags())
        clip_frames[vp] = sorted(cdir.glob("*.png"))

    # 2) Render frame by frame.
    log(f"[web] Rendering {total} frame(s) at {width}x{height} via headless three.js…")
    with sync_playwright() as pw:
        browser, page = _launch(pw, width + 40, height + 40)
        try:
            backend = page.evaluate("([w, h]) => window.api.init(w, h)", [width, height])
            log(f"[web] renderer backend: {backend}")
            info = page.evaluate("([b, bin]) => window.api.loadGLB(b, bin)", _load_args(glb))
            mat_names = set(info.get("materials", []))
            page.evaluate("(n) => window.api.useCamera(n)", job.target_camera or "")
            for mn, _ in mappings:
                if mn not in mat_names:
                    log(f"[web] WARNING: material '{mn}' not found in the scene — skipped.")
            canvas = page.query_selector("canvas")
            if canvas is None:
                raise RuntimeError("web renderer produced no canvas")
            for out_i, _frame in enumerate(range(fs, fe + 1)):
                if should_cancel and should_cancel():
                    log("[web] Cancelled.")
                    return 1
                for mn, vp in mappings:
                    if mn not in mat_names:
                        continue
                    frames = clip_frames.get(vp) or []
                    if not frames:
                        continue
                    ci = min(out_i, len(frames) - 1)   # hold last frame past clip end
                    data = base64.b64encode(frames[ci].read_bytes()).decode("ascii")
                    page.evaluate("([m, d]) => window.api.setEmissive(m, d)",
                                  [mn, f"data:image/png;base64,{data}"])
                page.evaluate("() => window.api.render()")
                canvas.screenshot(path=str(out_dir / f"out_{out_i:05d}.png"))
                if out_i % 20 == 0:
                    log(f"[web] frame {out_i + 1}/{total}")
        finally:
            browser.close()

    rendered = sorted(out_dir.glob("*.png"))
    if not rendered:
        raise RuntimeError("Web render produced no frames.")

    # 3) Assemble (movie via ffmpeg, or copy out an image sequence).
    out_path = Path(job.output_path).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm") or out_path.suffix == "":
        movie = out_path if out_path.suffix else out_path.with_suffix(".mp4")
        subprocess.run(
            [ff, "-y", "-framerate", str(fps), "-i", str(out_dir / "out_%05d.png"),
             "-pix_fmt", "yuv420p", str(movie)],
            check=True, capture_output=True, creationflags=subprocess_creation_flags())
        log(f"[web] Wrote {movie}")
    else:
        seq = out_path if out_path.suffix == "" else out_path.parent
        seq.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(rendered):
            shutil.copy2(f, seq / f"frame_{fs + i:05d}.png")
        log(f"[web] Wrote {len(rendered)} frames to {seq}")
    return 0
