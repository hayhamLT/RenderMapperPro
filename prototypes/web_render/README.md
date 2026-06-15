# Web render backend — proof of concept

A standalone spike for a **third render backend**: headless‑browser **three.js**
(WebGPU, auto‑falling back to WebGL2), driven by **Playwright (Python)**. It does
**not** touch `app_qt.py` or the Blender / C4D paths — it exists to prove the
novel/risky part before building the real backend.

## What it proves

The exact shape of the app's pipeline, end to end:

```
clip --ffmpeg--> PNG frames --three.js (headless Chrome)--> rendered frames --ffmpeg--> mp4
```

`scene.html` builds a three.js scene (a plane with an **unlit/emissive** material —
the same "full‑bright video" semantics as the Blender/Redshift mapping) and
exposes `renderFrame(pngDataUrl, angle)`. `render_poc.py` extracts a test clip to
PNGs (reusing the same ffmpeg frame‑extraction the C4D path already does), feeds
each frame to the page, captures the canvas, and assembles an mp4 with the
bundled ffmpeg.

## Verified result (this machine)

```
clip frames extracted: 48
backend used: webgl2     # WebGPU→WebGL2 fallback; a real GPU can use webgpu
frames rendered: 48
mp4: …/web_render_poc.mp4 (≈48 KB)
RESULT: OK
```

A sample rendered frame shows the test pattern textured onto the plane and
rotated in perspective — i.e. real 3D, not a passthrough.

## Run it

```bash
.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
.venv/bin/python prototypes/web_render/render_poc.py
```

## How it maps to the real backend

- **Dispatch by file type** — `.glb`/`.gltf` → a `web_worker.py` (same shape as
  `blender_worker.py` / `c4d_worker.py`), so it slots into the existing routing.
- **Scene** — load the `.glb` with three.js `GLTFLoader`; map clips onto material
  emissive maps by **name**, exactly like the current auto‑map.
- **Frames** — reuse the existing ffmpeg clip‑frame extraction (deterministic; no
  realtime `VideoTexture` drift).
- **Output** — hand rendered frames to the existing ffmpeg assembly + audio mux.
- **Bundling** — fetch Playwright's Chromium **on demand** via the same
  managed‑runtime pattern as the optional Blender download (don't fatten the base
  app).

## Caveats

- **GPU vs software** — Chrome is removing the SwiftShader software‑WebGL
  fallback; this spike forces it (`--use-angle=swiftshader`) so it runs anywhere.
  On a real‑GPU workstation, drop those flags for hardware accel (and a shot at
  WebGPU). Position this as a fast **local / web‑native** backend, not a farm one.
- **Dependency** — adds Playwright + a Chromium (~150 MB, on‑demand).
