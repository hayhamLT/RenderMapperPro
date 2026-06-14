# Render Mapper Pro

A standalone desktop app that maps videos onto 3D‑scene materials and renders them headlessly — built for LED‑wall / screen‑content workflows where one scene drives many video surfaces.

It supports **two render backends**, chosen automatically by scene type: **Blender** (`.blend`, `.fbx`, `.usd`, …) and **Cinema 4D + Redshift** (`.c4d`). It runs the renderer in the background, so a renderer crash can never take down the app, and ships bundled static `ffmpeg`/`ffprobe` so audio muxing and clip probing work out of the box.

![Render Mapper Pro](docs/screenshots/render-mapper-pro.png)

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

- Load a 3D scene — **Blender** (`.blend`, `.fbx`, `.obj`, `.glb`, `.usd`, `.abc`, …) or **Cinema 4D** (`.c4d`) — and one or many videos.
- **Scan** the scene to populate materials and cameras — and pull the scene's own render settings (fps, frame range, resolution, engine, samples, and for C4D the Redshift sampling/optimization) straight into the UI.
- Map videos onto materials (full‑bright **emission** or **base‑color/alpha**) — multiple video→material pairs render in a single pass.
- **Auto‑map by name:** clips link to materials automatically when the material name appears in the filename — on import or via a button (gap‑fill only, never clobbers manual mappings).
- **Watch folder:** point at a folder and dropped clips import + map themselves. Version‑aware (`Screen_v1`/`v2`/`_3` → latest wins) and auto‑updates the project to a newer version; half‑copied files are skipped until complete.
- **Auto‑render targets:** mark the screen materials a render must cover (right‑click → *Mark as Render Target*). When the watch folder fills every target — or a newer version arrives — a single multi‑screen render is queued automatically, named after the clips with a `PREVIZ` suffix. Queue‑only or auto‑start; all configurable in *Properties → General → Auto‑render*.
- **Automatic updates:** on launch the app checks GitHub Releases and offers a one‑click download of the right platform build — zero configuration. For this private repo, a read‑only token is baked into the build by CI (from the `RMP_UPDATE_TOKEN` Actions secret → `assets/update_token.txt`, which is git‑ignored); without it, auto‑update is simply off.
- **Burn‑in overlay** (optional): stamps the clip name/version, frame number, camera and date onto every frame — versioned previz reviews are never ambiguous (Blender + local C4D renders).
- **Auto‑retry:** a failed job automatically retries once (after the remaining jobs) — protects unattended auto‑renders from GPU/license hiccups.
- **Aspect guard:** warns when a clip's aspect ratio is far from its screen's (e.g. 16:9 footage on a 21:9 wall) — auto‑map's seatbelt.
- **Delivery copy:** finished renders can copy themselves into a delivery/review folder (*Properties → Watch & Auto‑render → Delivery*).
- **Rec.709 tagging:** movie outputs are colour‑tagged (+faststart) so they look identical in every player and NLE.
- Video textures render at **full quality** (cubic sampling, texture‑size limits forced off, no mip down‑scaling).
- Per‑clip **audio**: clips with sound show a speaker badge; click to mute/include per clip.
- **Renderer‑aware settings** — the panel adapts to the active engine so every control is real:
  - **Blender:** Cycles/EEVEE, samples, denoise, device, colour transform/exposure/gamma, transparent.
  - **Redshift:** Speed Preset (Draft→Final), Max/Min samples, adaptive Noise Threshold, denoise, GI bounces / on‑off, Max Ray Depth.
- Output profiles: H.264 MP4, ProRes MOV, PNG/EXR sequence.
- **Render farm (Thinkbox Deadline):** submit Blender *and* Cinema 4D jobs. C4D jobs are baked and rendered with the licensed Cinema 4D command‑line renderer; jobs carry the app icon in the Deadline Monitor and distribute frames across nodes. **Auto‑chunking** sizes frames‑per‑task from render history; *Deadline → Farm Nodes…* lists the farm; right‑click a job to **Set Priority** or **Requeue**.
- **Render analytics & cost:** every render records seconds/frame, total time and an estimated power **cost** (set wattage + rate in *Tools → Power & Cost*) — shown live and in *Tools → Render History*, with an upfront ETA from prior runs of the same scene.
- **Output review:** auto‑generated **contact sheets** for each render (preview them from History), plus a shareable **HTML report** with timing, cost and embedded thumbnails.
- **Notifications:** get pinged on render complete/fail via the system tray and/or a **Discord webhook** (*Tools → Notifications*) — everything also logs to Live Logs.
- **Command palette** (**⌘/Ctrl+K**) to search and run any action; **light/dark** theme toggle (*View → Light Theme*).

### Live Preview

- Pick any frame with the **frame scrubber** (prev/next, slider, frame field) and render just that frame — fast, at a chosen **render scale** (Full / ½ / ¼ / ⅛).
- **Auto** mode re‑renders the preview whenever you change the camera, resolution, frame, mapping, etc. (debounced, and coalesced so updates never pile up).
- **Display zoom** like After Effects: **double‑click toggles Fit ⇄ 100%**, centred on the clicked pixel; **grab to pan** at 100%. The image scales to the panel in Fit.
- Shows render **time and pixel size**; the finished movie plays (looped) in the same pane after a full render.

### Queue

- **Auto‑draft:** the moment you map a video it becomes a live job; edits save into it continuously — no "unsaved changes" limbo, no save dialog.
- Always exactly **one active job** (remembered across sessions); selecting a row opens it, switching never loses work.
- **Double‑click a job name to rename** (sticks; auto‑labels are tagged with the camera otherwise).
- New jobs are added at the **top**; **⌘/Ctrl+D** duplicates, **Delete** removes, right‑click for Duplicate / Set Priority / Requeue / Reveal / Open / Move / **Clear Queue**.
- Per‑row **Run** checkbox and progress; live, filterable logs (text + level filters).

## Project / preset files

- **Projects** — `.rmproj` (full setup: scene, clips, mappings, queue).
- **Presets** — `.rmpreset` (reusable render‑settings recipe).

Both are JSON under the hood. App data lives in `~/.blender_video_mapper/`:
`profile.json` (auto‑saved state), `presets/*.rmpreset`, `logs/app_qt.log`, `reports/`.

## Project structure

- `app_qt.py` — the Qt (PySide6) desktop UI.
- `theme.py` / `icons.py` — dark theme tokens + SVG icon set that drive all styling.
- `blender_worker.py` / `blender_discover.py` — headless Blender render + scene discovery.
- `c4d_worker.py` / `c4d_discover.py` — headless Cinema 4D + Redshift render/bake + discovery (run under `c4dpy`).
- `deadline/RenderMapperPro/` — custom Deadline plugin (app icon + cross‑platform C4D Commandline render); installed into the repository's `custom/plugins/`.
- `core/` — UI‑agnostic logic: `models.py` (job config), `runner.py` (subprocess + Deadline submission), `discovery.py`, `utils.py` (output paths, name auto‑match, version reconciliation).
- `tests/` — pytest suite for `core/` (matching, versioning, runner, Deadline submission).
- `BlenderVideoMapper.spec` — PyInstaller build spec (bundles both workers + ffmpeg).

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

## Cinema 4D + Redshift notes

- Requires Cinema 4D 2026 (auto‑detected via `c4dpy`) with Redshift; the clip is injected into the target material's **Redshift emission** as a full‑bright image sequence (Redshift can't read `.mp4`, so frames are extracted with the bundled `ffmpeg`).
- **Local renders/preview** run under `c4dpy`. **Farm renders** bake a self‑contained `.c4d` (relative sequence paths) and render it with the licensed Cinema 4D **Commandline** — so any node that already renders C4D works, with no extra licensing setup.
- **Farm prerequisites:** each render node needs Cinema 4D + Redshift licensed (as for the stock Cinema4D plugin). The `RenderMapperPro` Deadline plugin lives in `deadline/RenderMapperPro/` and is installed into the repository's `custom/plugins/`.
