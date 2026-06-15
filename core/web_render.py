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
import os
import re
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

# The headless Chromium lives in a writable per-user dir, shared by the on-demand
# install and the runtime launch. HARD-SET (not setdefault) before any Playwright
# import: in a frozen app Playwright's _transport does
# env.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0"), which would point browsers at
# the read-only bundle. This module is imported before any playwright import.
WEB_RUNTIME_ROOT = Path.home() / ".blender_video_mapper" / "browsers"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(WEB_RUNTIME_ROOT)

# --enable-unsafe-swiftshader: allow Chrome's software WebGL fallback so the
# backend renders on GPU-less/headless machines (a real GPU is used when present).
_CHROME_ARGS = ["--enable-unsafe-swiftshader"]

_PLAYWRIGHT_HINT = (
    "The web (three.js) backend needs Playwright. Install it with:\n"
    "    pip install playwright"
)


def web_chromium_installed() -> bool:
    """True if the headless Chromium shell is present in the managed dir.

    Globs the shell dir rather than using ``executable_path`` — the latter
    returns a path even when nothing is installed, and a default headless launch
    resolves to the *headless shell*, not full Chromium."""
    return any(WEB_RUNTIME_ROOT.glob("chromium_headless_shell-*"))


def ensure_web_chromium(on_log: LogCallback | None = None,
                        should_cancel: CancelCheck | None = None) -> None:
    """Download the headless Chromium shell into ``WEB_RUNTIME_ROOT`` if missing.

    Frozen-safe: shells out to Playwright's bundled node + cli.js (resolved via
    ``compute_driver_executable``), NOT ``python -m playwright`` (there is no
    python in a frozen .app). ``--only-shell`` fetches just the ~190 MB headless
    shell. Progress (a ``\\r``-delimited percent bar) streams to ``on_log``."""
    if web_chromium_installed():
        return
    log = on_log or (lambda *_: None)
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
    except Exception as exc:
        raise RuntimeError(_PLAYWRIGHT_HINT) from exc
    WEB_RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
    node, cli = compute_driver_executable()
    env = dict(get_driver_env())
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(WEB_RUNTIME_ROOT)
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "PLAYWRIGHT_DOWNLOAD_HOST"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    log("[web] Downloading the web render runtime (headless Chromium, ~190 MB, one time)…")
    proc = subprocess.Popen(
        [str(node), str(cli), "install", "--only-shell", "chromium"],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
        creationflags=subprocess_creation_flags())
    pat = re.compile(rb"(\d{1,3})%")
    buf, last = b"", -1
    assert proc.stdout is not None
    while True:
        if should_cancel and should_cancel():
            proc.terminate()
            raise RuntimeError("cancelled")
        chunk = proc.stdout.read(256)
        if not chunk:
            break
        buf += chunk
        while True:   # the progress bar uses \r, so split on \r and \n
            i = min((j for j in (buf.find(b"\r"), buf.find(b"\n")) if j != -1), default=-1)
            if i == -1:
                break
            line, buf = buf[:i], buf[i + 1:]
            m = pat.search(line)
            if m and int(m.group(1)) != last:
                last = int(m.group(1))
                log(f"[web] Chromium download {last}%")
    if proc.wait() != 0:
        raise RuntimeError("Chromium download failed — check your network and try again.")
    if not web_chromium_installed():
        raise RuntimeError("Chromium download finished but the runtime wasn't found.")
    log("[web] Web render runtime ready.")


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
    ensure_web_chromium(on_log)
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
    ensure_web_chromium(log, should_cancel)

    ff = _find_ffmpeg()
    r = job.render
    scale = max(1, int(getattr(r, "resolution_percentage", 100))) / 100.0
    width = max(1, int(r.width * scale))
    height = max(1, int(r.height * scale))
    fs, fe = int(r.frame_start), int(r.frame_end)
    fps = int(r.fps) or 24
    mappings = _clip_mappings(job)

    # Scene lighting config (web/three.js backend). getattr-with-defaults keeps
    # older saved jobs and the discovery warmup probe working unchanged.
    web_light_preset = str(getattr(r, "web_lighting_preset", "auto") or "auto")
    web_light_intensity = float(getattr(r, "web_lighting_intensity", 1.0) or 1.0)
    web_respect_lights = bool(getattr(r, "web_respect_scene_lights", True))

    # Single-frame preview vs. full render.
    preview_frame = int(getattr(job, "preview_frame", 0) or 0)
    out_frames = [preview_frame] if preview_frame > 0 else list(range(fs, fe + 1))
    total = len(out_frames)

    work = Path(tempfile.mkdtemp(prefix="webrender_"))
    out_dir = work / "out"
    out_dir.mkdir()

    # 1) Extract the clip frames we need: the whole clip for a full render, or
    #    just the one frame for a preview. ``clip_offset`` is the timeline offset
    #    that the extracted list's index 0 corresponds to.
    clip_frames: dict[str, list[Path]] = {}
    clip_offset: dict[str, int] = {}
    for _, vp in mappings:
        if vp in clip_frames:
            continue
        cdir = work / f"clip_{len(clip_frames)}"
        cdir.mkdir()
        if preview_frame > 0:
            idx = max(0, preview_frame - fs)
            log(f"[web] Extracting preview frame from {Path(vp).name}")
            subprocess.run([ff, "-y", "-i", vp, "-vf", f"select=eq(n\\,{idx})",
                            "-frames:v", "1", "-fps_mode", "vfr", str(cdir / "f_00000.png")],
                           check=True, capture_output=True, creationflags=subprocess_creation_flags())
            got = sorted(cdir.glob("*.png"))
            if not got:   # idx past the clip end → fall back to the first frame
                subprocess.run([ff, "-y", "-i", vp, "-frames:v", "1", str(cdir / "f_00000.png")],
                               check=True, capture_output=True, creationflags=subprocess_creation_flags())
                got = sorted(cdir.glob("*.png"))
            clip_frames[vp], clip_offset[vp] = got, idx
        else:
            log(f"[web] Extracting frames: {Path(vp).name}")
            subprocess.run([ff, "-y", "-i", vp, str(cdir / "f_%05d.png")],
                           check=True, capture_output=True, creationflags=subprocess_creation_flags())
            clip_frames[vp], clip_offset[vp] = sorted(cdir.glob("*.png")), 0

    # 2) Render.
    log(f"[web] Rendering {total} frame(s) at {width}x{height} via headless three.js…")
    with sync_playwright() as pw:
        browser, page = _launch(pw, width + 40, height + 40)
        try:
            backend = page.evaluate("([w, h]) => window.api.init(w, h)", [width, height])
            log(f"[web] renderer backend: {backend}")
            info = page.evaluate("([b, bin]) => window.api.loadGLB(b, bin)", _load_args(glb))
            mat_names = set(info.get("materials", []))
            page.evaluate("(n) => window.api.useCamera(n)", job.target_camera or "")
            # Configure scene lighting once, after the GLB + camera exist and
            # before the frame loop. A dict serializes to the JS cfg object.
            page.evaluate(
                "(cfg) => window.api.setLighting(cfg)",
                {"preset": web_light_preset, "intensity": web_light_intensity,
                 "respectSceneLights": web_respect_lights},
            )
            log(f"[web] lighting: preset={web_light_preset} "
                f"intensity={web_light_intensity} respectScene={web_respect_lights}")
            for mn, _ in mappings:
                if mn not in mat_names:
                    log(f"[web] WARNING: material '{mn}' not found in the scene — skipped.")
            canvas = page.query_selector("canvas")
            if canvas is None:
                raise RuntimeError("web renderer produced no canvas")
            for out_i, frame in enumerate(out_frames):
                if should_cancel and should_cancel():
                    log("[web] Cancelled.")
                    return 1
                for mn, vp in mappings:
                    if mn not in mat_names:
                        continue
                    frames = clip_frames.get(vp) or []
                    if not frames:
                        continue
                    ci = (frame - fs) - clip_offset.get(vp, 0)   # index into the extracted list
                    ci = min(max(0, ci), len(frames) - 1)        # hold last frame past clip end
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

    # 3) Output: a single PNG for a preview, else a movie or an image sequence.
    out_path = Path(job.output_path).expanduser()
    if preview_frame > 0:
        dest = out_path if out_path.suffix == "" else out_path.parent
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rendered[0], dest / f"preview_{preview_frame:05d}.png")
        log(f"[web] Preview frame {preview_frame} ready.")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in (".mp4", ".mov", ".mkv", ".webm"):
        subprocess.run(
            [ff, "-y", "-framerate", str(fps), "-i", str(out_dir / "out_%05d.png"),
             "-pix_fmt", "yuv420p", str(out_path)],
            check=True, capture_output=True, creationflags=subprocess_creation_flags())
        log(f"[web] Wrote {out_path}")
    else:
        seq = out_path if out_path.suffix == "" else out_path.parent
        seq.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(rendered):
            shutil.copy2(f, seq / f"frame_{fs + i:05d}.png")
        log(f"[web] Wrote {len(rendered)} frames to {seq}")
    return 0
