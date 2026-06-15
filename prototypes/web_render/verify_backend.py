"""End-to-end check of core/web_render.py: generate a tiny .gltf (a quad with a
material named 'Screen' and a camera 'RenderCam'), then run discovery + a real
render through run_web_job. Standalone — does not touch the app.

Run:  .venv/bin/python prototypes/web_render/verify_backend.py
"""
import base64
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from core.models import JobConfig, MaterialVideoAssignment, RenderOptions  # noqa: E402
from core.web_render import discover_web_scene, run_web_job  # noqa: E402


def make_test_gltf(path: Path) -> None:
    pos = [-1.6, -0.9, 0, 1.6, -0.9, 0, 1.6, 0.9, 0, -1.6, 0.9, 0]
    uv = [0, 1, 1, 1, 1, 0, 0, 0]
    idx = [0, 1, 2, 0, 2, 3]
    buf = struct.pack("<12f", *pos) + struct.pack("<8f", *uv) + struct.pack("<6H", *idx)
    uri = "data:application/octet-stream;base64," + base64.b64encode(buf).decode()
    gltf = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 1]}],
        "nodes": [
            {"mesh": 0, "name": "ScreenMesh"},
            {"camera": 0, "name": "RenderCam", "translation": [0, 0, 6]},
        ],
        "cameras": [{"type": "perspective", "perspective": {
            "yfov": 0.7, "aspectRatio": 1.777, "znear": 0.01, "zfar": 1000}}],
        "materials": [{"name": "Screen", "pbrMetallicRoughness": {
            "baseColorFactor": [0.05, 0.05, 0.05, 1], "metallicFactor": 0, "roughnessFactor": 1},
            "emissiveFactor": [0, 0, 0]}],
        "meshes": [{"primitives": [{
            "attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3",
             "min": [-1.6, -0.9, 0], "max": [1.6, 0.9, 0]},
            {"bufferView": 1, "componentType": 5126, "count": 4, "type": "VEC2"},
            {"bufferView": 2, "componentType": 5123, "count": 6, "type": "SCALAR"},
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": 48, "target": 34962},
            {"buffer": 0, "byteOffset": 48, "byteLength": 32, "target": 34962},
            {"buffer": 0, "byteOffset": 80, "byteLength": 12, "target": 34963},
        ],
        "buffers": [{"byteLength": len(buf), "uri": uri}],
    }
    path.write_text(json.dumps(gltf))


def main() -> int:
    from core.web_render import _find_ffmpeg
    ff = _find_ffmpeg()
    work = Path(tempfile.mkdtemp(prefix="webbackend_"))
    gltf = work / "screen.gltf"
    make_test_gltf(gltf)
    clip = work / "clip.mp4"
    subprocess.run([ff, "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x180:rate=24",
                    "-pix_fmt", "yuv420p", str(clip)], check=True, capture_output=True)

    logs: list[str] = []
    log = logs.append

    # 1) Discovery
    mats, cams, _ = discover_web_scene(str(gltf), log)
    print("discovered materials:", mats, "cameras:", cams)
    assert mats == ["Screen"], mats
    assert cams == ["RenderCam"], cams

    # 2) Render
    out = work / "out.mp4"
    job = JobConfig(
        scene_path=str(gltf), video_path=str(clip), target_material="Screen",
        target_camera="RenderCam", output_path=str(out),
        render=RenderOptions(width=480, height=270, fps=24, frame_start=1, frame_end=24),
        material_assignments=[MaterialVideoAssignment("Screen", str(clip))],
    )
    rc = run_web_job(job, log)
    for ln in logs:
        print("  ", ln)
    assert rc == 0, f"run_web_job rc={rc}"
    assert out.exists() and out.stat().st_size > 0, "no output movie"
    print(f"\nOUTPUT: {out} ({out.stat().st_size} bytes)")

    # Save a frame for visual inspection.
    framesdir = work / "frames"
    framesdir.mkdir()
    subprocess.run([ff, "-y", "-i", str(out), "-frames:v", "1", "-ss", "0.5",
                    str(framesdir / "f.png")], check=True, capture_output=True)
    sample = framesdir / "f.png"
    if sample.exists():
        dest = Path("/tmp/web_backend_frame.png")
        dest.write_bytes(sample.read_bytes())
        print(f"sample frame: {dest}")
    print("\nRESULT: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
