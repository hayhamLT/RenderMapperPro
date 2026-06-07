# Blender Video Texture Mapper (Desktop App)

A standalone desktop application that automates video texture mapping and headless rendering in Blender.

## What It Does

- Pick one 3D scene file (`.blend`, `.fbx`, `.obj`, `.glb`, etc.)
- Pick one or many video files
- Scan scene to auto-populate material and camera dropdowns with live import logs and cached results
- Map multiple videos to multiple materials in a single render pass
- Drive materials through a full-bright emission mapping mode for LED/screen style playback
- Set render size, FPS, frame range, output path, engine, and cycles samples
- Configure output profiles (H264 MP4, ProRes MOV, PNG sequence, OpenEXR sequence)
- Configure frame step, color-management settings, timeout policies, retry count, and safety toggles
- Optionally auto-fallback to PNG sequence if movie profile render fails
- Run Blender in background mode (`-b`) and render final MP4 output
- Show live logs while rendering
- Manage a job queue with status, retry failed jobs, cancel current run, and export per-job logs
- Run a Dry Run preflight validator before rendering
- Export diagnostics and structured run reports

## Project Structure

- `app.py`: Tkinter desktop UI
- `blender_worker.py`: Blender automation script executed in headless mode
- `blender_discover.py`: Blender discovery script to list scene materials/cameras
- `core/models.py`: Dataclasses for job configuration
- `core/runner.py`: Subprocess execution and log streaming
- `core/utils.py`: Output path and validation helpers
- `core/discovery.py`: Discovery subprocess and parser

## Requirements

- macOS, Linux, or Windows
- Python 3.10+
- Blender installed

On this macOS environment, prefer `~/.local/bin/python3.12` over the system `python3` so Tk uses the newer runtime.

## Run From Source

1. Install dependencies:

```bash
~/.local/bin/python3.12 -m pip install -r requirements.txt
```

If `~/.local/bin/python3.12` is not available on your machine, use any Python 3.10+ runtime instead.

2. Launch the app:

```bash
~/.local/bin/python3.12 app.py
```

3. In the app:

- Set Blender executable:
  - macOS example: `/Applications/Blender.app/Contents/MacOS/Blender`
- Select a scene file
- Click **Scan Scene** to load available materials and cameras
- Add one or many video files
- Pick a target camera
- For simple batch mode: leave the mapping table empty, pick one material, and render each selected video against that material
- For multi-material scenes: add one mapping row per material/video pair and keep the default **Emission Full Bright** mode for fully visible screens
- Set render settings and output path
- Click **Start Headless Render**

Queue tools:

- **Retry Failed**: rerun failed/cancelled jobs
- **Cancel Current**: stop active Blender process safely
- **Export Selected Log**: save logs for one queue item

Top menu tools:

- **Profile**
  - Save Named Preset
  - Load Named Preset
  - Reset To Defaults
- **Tools**
  - Dry Run (Validate Only)
  - Copy Diagnostics
  - Open Last Run Report

## Batch Behavior

- If one video is selected:
  - Output path can be an `.mp4` file or a directory
- If multiple videos are selected:
  - Output path is treated as a directory
  - Output files are generated as:
    - `<scene_name>__<video_name>.mp4`

## Build Standalone App (PyInstaller)

```bash
chmod +x build_standalone.sh
./build_standalone.sh
```

The build script automatically prefers `~/.local/bin/python3.12`, then `python3.12`, then `python3`.
It creates a local build virtual environment such as `.build-venv-py3.12` so PyInstaller and related tooling do not need to be installed into your system Python.
You can force a specific interpreter with `BLENDER_VIDEO_MAPPER_PYTHON=/path/to/python ./build_standalone.sh`.

Binary output:

- `dist/Render Mapper Pro.app` (macOS app bundle, named and icon'd as Render Mapper Pro)

## Blender Notes

- Material must exist and contain a Principled BSDF node
- Camera name must match an existing camera object
- Worker script creates/updates `AUTO_VIDEO_*` nodes for each mapped material
- Emission Full Bright mode connects the video directly to an emission shader so the source video stays white-balanced and fully visible
- Render output is configured to H.264 MP4 via Blender FFMPEG settings
- Timeout and idle-timeout controls can terminate stalled Blender runs
- Safe mode performs basic path/extension validation in the worker script
- Runner drains final Blender stdout lines so late worker errors are captured in logs

## Reports and Logs

- Application log (rotating): `~/.blender_video_mapper/logs/app.log`
- Auto-saved profile: `~/.blender_video_mapper/profile.json`
- Named presets: `~/.blender_video_mapper/presets/*.json`
- Run reports: `~/.blender_video_mapper/reports/run_report_*.json`

## Troubleshooting

- `Material not found`: Check target material name spelling/case
- `Camera not found`: Check camera object name
- Blender executable not found: Set full path to Blender binary
- Non-zero exit code: inspect live logs for import/render errors
- Scene scan appears stuck: the app now streams Blender discovery logs and caches successful scans so preflight/start-render do not rescan unchanged scenes
- MP4/MOV failures with success in PNG sequence usually indicate local codec/ffmpeg/runtime constraints in Blender
