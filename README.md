# Render Mapper Pro

A standalone desktop app that maps videos onto Blender materials and renders them headlessly — built for LED‑wall / screen‑content workflows where one scene drives many video surfaces.

It runs Blender in the background (`-b`), so a Blender crash can never take down the app, and ships a bundled static `ffmpeg`/`ffprobe` so audio muxing and clip probing work out of the box.

## Download

Grab a prebuilt app from the [**Releases**](../../releases) page:

| Platform | File |
|----------|------|
| macOS (Apple Silicon) | `RenderMapperPro-macOS-arm64.zip` |
| macOS (Intel) | `RenderMapperPro-macOS-intel.zip` |
| Windows (x64) | `RenderMapperPro-Windows-x64.zip` |

- **macOS:** unzip, then right‑click the app → **Open** → **Open** (first launch only — it isn't notarized).
- **Windows:** unzip, run **Render Mapper Pro.exe**. If SmartScreen warns, **More info → Run anyway**.

Every push to `main` also publishes the three builds as downloadable **workflow artifacts** under the GitHub Actions run.

## What it does

- Load a 3D scene (`.blend`, `.fbx`, `.obj`, `.glb`, `.usd`, `.abc`, …) and one or many videos.
- **Scan** the scene to populate materials and cameras — and pull the scene's own render settings (fps, frame range, resolution, engine, samples, colour management) straight into the UI.
- Map videos onto materials (full‑bright **emission** or **base‑color/alpha**) — multiple video→material pairs render in a single pass.
- Video textures render at **full quality** (cubic sampling, texture‑size limits forced off, no mip down‑scaling).
- Per‑clip **audio**: clips with sound show a speaker badge; click to mute/include per clip.
- Configure resolution, fps, frame range/step, engine (Cycles/EEVEE), samples, denoise, colour transform/exposure/gamma, output profile (H.264 MP4, ProRes MOV, PNG/EXR sequence, …).
- Optional **Thinkbox Deadline** farm submission.

### Live Preview

- Pick any frame with the **frame scrubber** (prev/next, slider, frame field) and render just that frame — fast, at a chosen **render scale** (Full / ½ / ¼ / ⅛).
- **Auto** mode re‑renders the preview whenever you change the camera, resolution, frame, mapping, etc. (debounced, and coalesced so updates never pile up).
- **Display zoom** like After Effects: **double‑click toggles Fit ⇄ 100%**, centred on the clicked pixel; **grab to pan** at 100%. The image scales to the panel in Fit.
- Shows render **time and pixel size**; the finished movie plays (looped) in the same pane after a full render.

### Queue

- **Auto‑draft:** the moment you map a video it becomes a live job; edits save into it continuously — no "unsaved changes" limbo, no save dialog.
- Always exactly **one active job** (remembered across sessions); selecting a row opens it, switching never loses work.
- **Double‑click a job name to rename** (sticks; auto‑labels are tagged with the camera otherwise).
- New jobs are added at the **top**; **⌘/Ctrl+D** duplicates, **Delete** removes, right‑click for Duplicate / Reveal / Open / Move / **Clear Queue**.
- Per‑row **Run** checkbox and progress; live, filterable logs (text + level filters).

## Project / preset files

- **Projects** — `.rmproj` (full setup: scene, clips, mappings, queue).
- **Presets** — `.rmpreset` (reusable render‑settings recipe).

Both are JSON under the hood. App data lives in `~/.blender_video_mapper/`:
`profile.json` (auto‑saved state), `presets/*.rmpreset`, `logs/app_qt.log`, `reports/`.

## Project structure

- `app_qt.py` — the Qt (PySide6) desktop UI.
- `theme.py` / `icons.py` — dark theme tokens + SVG icon set that drive all styling.
- `blender_worker.py` — headless render script run inside Blender.
- `blender_discover.py` — scene discovery (materials, cameras, render settings).
- `core/` — UI‑agnostic logic: `models.py` (job config), `runner.py` (subprocess + Deadline), `discovery.py`, `utils.py`.
- `tests/` — pytest suite for `core/`.
- `BlenderVideoMapper.spec` — PyInstaller build spec.

## Run from source

Requires Python 3.12 and Blender installed.

```bash
python -m pip install -r requirements-build.txt   # PySide6 + PyInstaller
python tools/fetch_ffmpeg.py                       # vendored ffmpeg/ffprobe
python app_qt.py
```

Then set the Blender executable in **Properties** (e.g. `/Applications/Blender.app/Contents/MacOS/Blender`), pick a scene, **Scan**, add videos, map them, and use **Preview Frame** / the queue.

## Build a standalone app

```bash
python -m pip install -r requirements-build.txt
python tools/fetch_ffmpeg.py
python -m PyInstaller --noconfirm --clean BlenderVideoMapper.spec
# → dist/Render Mapper Pro.app  (macOS)  /  dist/Render Mapper Pro/  (Windows)
```

GitHub Actions (`.github/workflows/build.yml`) does this for macOS arm64/Intel + Windows on every push to `main`; pushing a `v*` tag also publishes a Release with the zips.

## Development

```bash
pip install pytest ruff
pytest -q                                           # unit tests
ruff check core tests blender_worker.py blender_discover.py
```

CI runs the same lint + tests as a gate before the build matrix.

## Blender notes

- The worker creates/updates `AUTO_VIDEO_TEXTURE` nodes per mapped material; emission mode wires the clip to an emission shader so screens stay fully visible.
- Video clips map over the full timeline; the single‑frame preview renders one scene frame while keeping that timeline in sync.
- Timeout / idle‑timeout controls can terminate stalled runs; safe mode validates paths/extensions in the worker.
