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

# Prefer the real GPU. Plain headless Chrome force-disables the GPU process and
# lands on SwiftShader (software). Running the FULL chromium in "new headless"
# (headless=False + --headless=new — the full build, windowless) lets ANGLE bind
# the platform GPU (Metal on macOS). The extra flags are defensive: Chrome binds
# the GPU on its own here, but they guard against driver blocklisting.
_GPU_LAUNCH = {"headless": False,
               "args": ["--headless=new", "--ignore-gpu-blocklist", "--use-angle=metal"]}
# Deliberate software fallback for GPU-less / no-GUI-session / blocklisted machines.
# Chrome 137+ dropped the automatic SwiftShader fallback, so it must be explicit.
_SOFTWARE_LAUNCH = {"headless": False,
                    "args": ["--headless=new", "--enable-unsafe-swiftshader",
                             "--use-angle=swiftshader"]}

# Reads the true GL device string; "SwiftShader"/"software"/empty ⇒ no real GPU.
_GL_PROBE_JS = """() => {
  try {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl2') || c.getContext('webgl');
    if (!gl) return '';
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    return (ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)
                : gl.getParameter(gl.RENDERER)) || '';
  } catch (e) { return ''; }
}"""


def _is_software_renderer(s: str) -> bool:
    s = (s or "").lower()
    return (not s) or ("swiftshader" in s) or ("software" in s)

_PLAYWRIGHT_HINT = (
    "The web (three.js) backend needs Playwright. Install it with:\n"
    "    pip install playwright"
)


def web_chromium_installed() -> bool:
    """True if the FULL Chromium build is present in the managed dir.

    Globs ``chromium-*`` (not ``chromium_headless_shell-*``): only the full build
    carries the GPU/ANGLE stack, so the headless shell — which is locked to
    SwiftShader — does not count as installed for this GPU-capable backend."""
    return any(WEB_RUNTIME_ROOT.glob("chromium-[0-9]*"))


def ensure_web_chromium(on_log: LogCallback | None = None,
                        should_cancel: CancelCheck | None = None) -> None:
    """Download the full Chromium build into ``WEB_RUNTIME_ROOT`` if missing.

    Frozen-safe: shells out to Playwright's bundled node + cli.js (resolved via
    ``compute_driver_executable``), NOT ``python -m playwright`` (there is no
    python in a frozen .app). ``--no-shell`` fetches the full ~170 MB GPU-capable
    Chromium (NOT the SwiftShader-only headless shell, which can't reach the GPU).
    Progress (a ``\\r``-delimited percent bar) streams to ``on_log``."""
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
    log("[web] Downloading the web render runtime (GPU-capable Chromium, ~170 MB, one time)…")
    proc = subprocess.Popen(
        [str(node), str(cli), "install", "--no-shell", "chromium"],
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


def _launch(pw, viewport_w: int, viewport_h: int, on_log: LogCallback | None = None):
    """Launch the scene page on the GPU when possible, else fall back to software.

    Tries the full chromium in new-headless (GPU-capable) first and verifies via
    UNMASKED_RENDERER that a real GPU bound; if it silently lands on SwiftShader
    (GPU-less box, no logged-in GUI session, blocklisted driver) it relaunches in
    explicit software mode. Returns (browser, page); logs the chosen backend."""
    log = on_log or (lambda *_: None)
    last_exc: Exception | None = None
    for mode, cfg in (("gpu", _GPU_LAUNCH), ("software", _SOFTWARE_LAUNCH)):
        try:
            browser = pw.chromium.launch(**cfg)
        except Exception as exc:   # full build missing / launch failure
            last_exc = exc
            continue
        try:
            page = browser.new_page(viewport={"width": viewport_w, "height": viewport_h})
            page.goto(_resolve_scene_html().as_uri())
            page.wait_for_function("window.__ready === true || window.__error !== ''", timeout=60000)
            err = page.evaluate("window.__error")
            if err:
                raise RuntimeError(f"web scene page failed to initialize: {err}")
            renderer = str(page.evaluate(_GL_PROBE_JS) or "")
            if mode == "gpu" and _is_software_renderer(renderer):
                browser.close()        # GPU path fell back to software — try explicit software
                continue
            if _is_software_renderer(renderer):
                log("[web] GPU unavailable — rendering in software (SwiftShader, slow)")
            else:
                log(f"[web] GPU active — {renderer}")
            return browser, page
        except Exception as exc:
            last_exc = exc
            try:
                browser.close()
            except Exception:
                pass
    raise RuntimeError(f"web scene failed to launch: {last_exc}")


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
        browser, page = _launch(pw, 96, 96, on_log)
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
        browser, page = _launch(pw, width + 40, height + 40, log)
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
