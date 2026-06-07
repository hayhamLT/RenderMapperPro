from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy


def log(message: str) -> None:
    print(f"[discover] {message}")


def parse_scene_path() -> Path:
    if "--" not in sys.argv:
        raise RuntimeError("Missing scene path. Expected '-- <scene_path>'")

    idx = sys.argv.index("--")
    args = sys.argv[idx + 1 :]
    if not args:
        raise RuntimeError("Scene path missing after '--'")

    scene_path = Path(args[0]).expanduser().resolve()
    if not scene_path.exists():
        raise FileNotFoundError(f"Scene file does not exist: {scene_path}")
    return scene_path


def load_scene(scene_path: Path) -> None:
    ext = scene_path.suffix.lower()

    if ext == ".blend":
        bpy.ops.wm.open_mainfile(filepath=str(scene_path))
        return

    bpy.ops.wm.read_factory_settings(use_empty=True)

    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(scene_path))
    elif ext in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(scene_path))
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=str(scene_path))
        else:
            bpy.ops.import_scene.obj(filepath=str(scene_path))
    elif ext in {".usd", ".usda", ".usdc"}:
        bpy.ops.wm.usd_import(filepath=str(scene_path))
    elif ext == ".abc":
        bpy.ops.wm.alembic_import(filepath=str(scene_path))
    elif ext == ".stl":
        bpy.ops.import_mesh.stl(filepath=str(scene_path))
    elif ext == ".ply":
        bpy.ops.import_mesh.ply(filepath=str(scene_path))
    else:
        raise RuntimeError(f"Unsupported scene extension: {ext}")


def discover_settings() -> dict:
    """Read render/timeline/colour settings straight from the .blend so the app
    can mirror what the artist set up in the scene."""
    s: dict = {}
    try:
        scene = bpy.context.scene
        r = scene.render
        # Effective fps (accounts for NTSC bases like 23.976 → fps 24 / base 1.001).
        base = getattr(r, "fps_base", 1.0) or 1.0
        s["fps"] = int(round(r.fps / base))
        s["frame_start"] = int(scene.frame_start)
        s["frame_end"] = int(scene.frame_end)
        s["frame_step"] = int(getattr(scene, "frame_step", 1))
        s["width"] = int(r.resolution_x)
        s["height"] = int(r.resolution_y)
        s["resolution_percentage"] = int(r.resolution_percentage)
        s["engine"] = str(r.engine)
        s["film_transparent"] = bool(getattr(r, "film_transparent", False))
        # Engine-specific sample counts.
        eng = str(r.engine)
        if eng == "CYCLES" and hasattr(scene, "cycles"):
            s["samples"] = int(getattr(scene.cycles, "samples", 64))
            s["use_denoise"] = bool(getattr(scene.cycles, "use_denoising", False))
        elif "EEVEE" in eng and hasattr(scene, "eevee"):
            s["samples"] = int(getattr(scene.eevee, "taa_render_samples", 64))
        # Colour management.
        vs = getattr(scene, "view_settings", None)
        if vs is not None:
            s["view_transform"] = str(vs.view_transform)
            s["look"] = str(vs.look)
            s["exposure"] = float(vs.exposure)
            s["gamma"] = float(vs.gamma)
    except Exception as exc:  # never let settings probing break discovery
        log(f"settings probe warning: {exc}")
    return s


def discover() -> dict:
    materials = sorted({m.name for m in bpy.data.materials if m is not None and m.name})
    cameras = sorted({obj.name for obj in bpy.data.objects if obj and obj.type == "CAMERA"})
    return {"materials": materials, "cameras": cameras, "settings": discover_settings()}


def main() -> None:
    scene_path = parse_scene_path()
    log(f"Loading scene: {scene_path}")
    load_scene(scene_path)
    result = discover()
    print("DISCOVERY_JSON:" + json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
