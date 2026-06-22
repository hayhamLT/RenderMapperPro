from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import bpy

VIDEO_MAPPING_MODE_EMISSION = "EMISSION_FULL_BRIGHT"
VIDEO_MAPPING_MODE_BASE_COLOR = "BASE_COLOR_ALPHA"

_RENDER_TEMP_DIR: Path | None = None

# Set when Blender can't enable its internal FFMPEG movie writer (the 5.x
# file_format setter rejects 'FFMPEG' and the ctypes byte-poke fails on some
# builds). We then render a PNG sequence and mux it to the movie ourselves with
# the bundled ffmpeg — the same approach the web/C4D backends already use — so a
# movie render never hard-fails on a Blender-version quirk.
_MOVIE_MUX: dict | None = None


def _worker_ffmpeg(config: dict) -> str:
    """The ffmpeg binary for the movie fallback: the bundled path the app passes,
    else the copy beside this worker (vendor/ffmpeg/<platform>/), else PATH."""
    p = str(config.get("ffmpeg_path", "")).strip()
    if p and Path(p).exists():
        return p
    plat = {"darwin": "darwin", "win32": "windows"}.get(sys.platform, "linux")
    arch = "arm64" if (os.uname().machine if hasattr(os, "uname") else "").lower() in ("arm64", "aarch64") else "x64"
    for cand in (Path(__file__).resolve().parent / "vendor" / "ffmpeg" / f"{plat}-{arch}"
                 / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"),):
        if cand.exists():
            return str(cand)
    return shutil.which("ffmpeg") or "ffmpeg"


def _audio_av_args(audio_paths, video_filter):
    """ffmpeg (audio_inputs, av_out) to mux source-clip audio onto a movie whose
    video is input 0 — kept in step with core.utils.ffmpeg_movie_av_args (this
    worker is standalone under Blender's Python and can't import core)."""
    paths = [str(p) for p in (audio_paths or []) if p]
    if not paths:
        return [], (["-vf", video_filter] if video_filter else [])
    audio_inputs = []
    for p in paths:
        audio_inputs += ["-i", p]
    vchain = f"[0:v]{video_filter}[v]" if video_filter else "[0:v]null[v]"
    if len(paths) == 1:
        fc = f"{vchain};[1:a]anull[a]"
    else:
        amix = "".join(f"[{i + 1}:a]" for i in range(len(paths)))
        fc = f"{vchain};{amix}amix=inputs={len(paths)}:duration=longest[a]"
    return audio_inputs, ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                          "-c:a", "aac", "-b:a", "192k", "-shortest"]


def _mux_sequence_to_movie(ffmpeg, frames_dir, fps, out_file, out_fmt, codec,
                           quality, audio_paths=None) -> None:
    """Assemble rendered PNG frames (any zero-padding) into a movie with the chosen
    codec/quality, muxing unmuted source-clip audio if present. Raises on failure."""
    crf = {"LOSSLESS": "0", "HIGH": "18", "MEDIUM": "23", "LOW": "28", "LOWEST": "32"}
    Path(out_file).parent.mkdir(parents=True, exist_ok=True)
    # Force even dimensions — H.264/H.265 with yuv420p reject odd width/height.
    even = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    audio_inputs, av_out = _audio_av_args(audio_paths, even)
    # glob input — padding-agnostic, so Blender's frame numbering can't break it.
    cmd = [ffmpeg, "-y", "-framerate", str(fps or 30),
           "-pattern_type", "glob", "-i", os.path.join(frames_dir, "*.png"),
           *audio_inputs, *av_out]
    if str(codec).upper() == "PRORES" or str(out_fmt).upper() == "QUICKTIME":
        cmd += ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]
    else:
        cmd += ["-c:v", "libx265" if str(codec).upper() == "H265" else "libx264",
                "-crf", crf.get(str(quality).upper(), "18"), "-pix_fmt", "yuv420p"]
    cmd += ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-movflags", "+faststart", str(out_file)]
    log(f"[fallback] Muxing PNG sequence -> {out_file}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if not (Path(out_file).exists() and Path(out_file).stat().st_size > 0):
        raise RuntimeError(f"ffmpeg failed to assemble the movie: {(r.stderr or '')[-600:]}")


def _render_temp_dir() -> Path:
    """Per-process local temp dir for Deadline renders. The uuid suffix avoids
    PID-recycling collisions with a stale dir a crashed earlier task left behind."""
    global _RENDER_TEMP_DIR
    if _RENDER_TEMP_DIR is None:
        _RENDER_TEMP_DIR = (
            Path(tempfile.gettempdir()) / f"blender_render_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        )
    return _RENDER_TEMP_DIR


def _cleanup_render_temp() -> None:
    """Remove the local temp dir if one was created — guaranteed cleanup, even
    when a render fails before the copy-out step."""
    if _RENDER_TEMP_DIR is not None:
        shutil.rmtree(_RENDER_TEMP_DIR, ignore_errors=True)


def _resolve_path(path_str: str) -> str:
    """Resolve a file path, falling back to the script's own directory and CWD.

    When running as a Deadline auxiliary file, all submitted files (scene,
    videos, worker script, config JSON) land in the same task directory next
    to this script.  If the stored absolute path doesn't exist on the current
    machine (e.g. a mounted drive that isn't present on this worker), we look
    for the file by *name* next to this script, then in the process CWD.
    """
    p = Path(path_str).expanduser()
    if p.exists():
        return str(p.resolve())
    name = p.name
    if not name:
        return path_str
    # 1. Look next to this script (Deadline auxiliary-files directory)
    script_dir = Path(__file__).resolve().parent
    candidate = script_dir / name
    if candidate.exists():
        return str(candidate)
    # 2. Look in the process working directory
    cwd_candidate = Path.cwd() / name
    if cwd_candidate.exists():
        return str(cwd_candidate)
    # Return the original (caller will raise a meaningful error)
    return path_str

def log(message: str) -> None:
    print(f"[worker] {message}")


def parse_config_path() -> Path:
    if "--" not in sys.argv:
        raise RuntimeError("Missing config path. Expected '-- <path_to_config.json>'")

    idx = sys.argv.index("--")
    args = sys.argv[idx + 1 :]
    if not args:
        raise RuntimeError("Config path argument missing after '--'")

    config_path = Path(args[0]).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file does not exist: {config_path}")
    return config_path


def _parse_task_frames():
    """Optional 'STARTFRAME ENDFRAME' trailing args after the config path —
    Deadline substitutes a task's chunk there. Returns (start, end) or None.
    If the tokens weren't substituted (non-Deadline run), returns None so the
    full configured range is used."""
    if "--" not in sys.argv:
        return None
    extra = sys.argv[sys.argv.index("--") + 1:][1:]   # everything after the config path
    nums = [a for a in extra if a.lstrip("-").isdigit()]
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return None


def load_scene(scene_path: str) -> None:
    path = Path(_resolve_path(scene_path))
    ext = path.suffix.lower()

    if ext == ".blend":
        log(f"Opening blend file: {path}")
        bpy.ops.wm.open_mainfile(filepath=str(path))
        return

    log("Resetting to empty scene before import")
    bpy.ops.wm.read_factory_settings(use_empty=True)

    log(f"Importing mesh/scene file: {path}")
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=str(path))
    elif ext in {".glb", ".gltf"}:
        bpy.ops.import_scene.gltf(filepath=str(path))
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=str(path))
        else:
            bpy.ops.import_scene.obj(filepath=str(path))
    elif ext in {".usd", ".usda", ".usdc"}:
        bpy.ops.wm.usd_import(filepath=str(path))
    elif ext == ".abc":
        bpy.ops.wm.alembic_import(filepath=str(path))
    elif ext == ".stl":
        bpy.ops.import_mesh.stl(filepath=str(path))
    elif ext == ".ply":
        bpy.ops.import_mesh.ply(filepath=str(path))
    else:
        raise RuntimeError(f"Unsupported scene extension: {ext}")


def ensure_lighting(config: dict) -> None:
    """Imported scenes (glTF/FBX/OBJ/USD…) usually ship NO lights and no lit world,
    so Cycles/EEVEE render them pure black — only emissive materials (a mapped
    video screen) show. When the imported scene has no light objects, add a neutral
    studio world + a key sun so it renders LIT, matching the three.js backend which
    adds its own lighting rig. The file's own lights are kept when present
    (respect_scene_lights); preset 'none' leaves it unlit on purpose."""
    scene = bpy.context.scene
    render = config.get("render", {})
    respect = bool(render.get("web_respect_scene_lights", True))
    intensity = float(render.get("web_lighting_intensity", 1.0) or 1.0)
    preset = str(render.get("web_lighting_preset", "auto") or "auto").lower()

    if any(o.type == "LIGHT" for o in scene.objects) and respect:
        log("Scene ships its own lights — keeping them")
        return
    if preset == "none":
        log("Lighting preset 'none' — leaving the scene unlit (emissive only)")
        return

    log(f"Imported scene has no lights — adding a neutral studio rig "
        f"(preset={preset}, intensity={intensity:.2f}) so it isn't black")
    # Neutral world ambient so surfaces aren't pure black.
    world = scene.world
    if world is None:
        world = bpy.data.worlds.new("RMP_World")
        scene.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    if bg is None:
        bg = world.node_tree.nodes.new("ShaderNodeBackground")
    amb = 0.55 if preset == "flat" else 0.20      # flat = brighter even ambient
    bg.inputs[0].default_value = (amb, amb, amb, 1.0)
    bg.inputs[1].default_value = max(0.0, intensity)
    if preset == "flat":
        return                                     # flat = ambient only, no directional key
    # A key sun for dimensional shading (rotation ≈ 50°/11°/40°).
    key = bpy.data.lights.new("RMP_Key", type="SUN")
    key.energy = 4.0 * intensity
    obj = bpy.data.objects.new("RMP_Key", key)
    obj.rotation_euler = (0.87, 0.20, 0.70)
    scene.collection.objects.link(obj)


def _material_output_node(nodes: bpy.types.Nodes) -> bpy.types.Node:
    for node in nodes:
        if node.type == "OUTPUT_MATERIAL" and getattr(node, "is_active_output", False):
            return node

    for node in nodes:
        if node.type == "OUTPUT_MATERIAL":
            return node

    return nodes.new(type="ShaderNodeOutputMaterial")


def _principled_node(nodes: bpy.types.Nodes) -> bpy.types.Node | None:
    for node in nodes:
        if node.type == "BSDF_PRINCIPLED":
            return node
    return None


def _clear_auto_video_nodes(nodes: bpy.types.Nodes) -> None:
    for node in list(nodes):
        if node.name.startswith("AUTO_VIDEO_"):
            nodes.remove(node)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".bmp", ".webp", ".tga", ".hdr"}


def _is_still_image(path: str) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def _create_movie_texture_node(
    nodes: bpy.types.Nodes,
    video_path: str,
    frame_start: int,
    frame_end: int,
    location: tuple[float, float],
) -> bpy.types.Node:
    tex = nodes.new(type="ShaderNodeTexImage")
    tex.name = "AUTO_VIDEO_TEXTURE"
    tex.label = "AUTO_VIDEO_TEXTURE"
    tex.location = location
    # Highest-quality sampling for screen content: cubic magnification, no
    # tiling/repeat artefacts at the edges.
    try:
        tex.interpolation = "Cubic"
        tex.extension = "EXTEND"
    except Exception:
        pass

    resolved = str(Path(_resolve_path(video_path)).resolve())
    image = bpy.data.images.load(filepath=resolved, check_existing=True)
    if _is_still_image(resolved):
        # A still image (e.g. a logo on one screen) maps as a static texture.
        image.source = "FILE"
        tex.image = image
        return tex

    image.source = "MOVIE"
    tex.image = image
    tex.image_user.use_auto_refresh = True
    tex.image_user.use_cyclic = True
    tex.image_user.frame_start = frame_start
    tex.image_user.frame_duration = max(1, frame_end - frame_start + 1)
    return tex


def _apply_cycles_device(scene, device: str) -> None:
    """device: AUTO | CPU | GPU. Best-effort GPU enablement (Metal/CUDA/OptiX…)."""
    try:
        if device == "CPU":
            scene.cycles.device = "CPU"
            log("Cycles device: CPU")
            return
        prefs = bpy.context.preferences.addons.get("cycles")
        enabled = 0
        if prefs is not None:
            cprefs = prefs.preferences
            for backend in ("METAL", "OPTIX", "CUDA", "HIP", "ONEAPI"):
                try:
                    cprefs.compute_device_type = backend
                    break
                except Exception:
                    continue
            try:
                cprefs.get_devices()
            except Exception:
                pass
            for dev in getattr(cprefs, "devices", []):
                if dev.type != "CPU":
                    dev.use = True
                    enabled += 1
                else:
                    dev.use = (device == "AUTO")
        if enabled > 0:
            scene.cycles.device = "GPU"
            log(f"Cycles device: GPU ({getattr(cprefs, 'compute_device_type', '?')}, {enabled} device(s))")
        else:
            scene.cycles.device = "GPU" if device == "GPU" else "CPU"
            log(f"Cycles device: {scene.cycles.device} (no GPU enumerated)")
    except Exception as exc:
        log(f"Device selection fell back to CPU: {exc}")
        try:
            scene.cycles.device = "CPU"
        except Exception:
            pass


def _apply_ambient_occlusion(scene, render, log) -> None:
    """Enable ambient occlusion when requested — adds contact-shadow depth where
    surfaces meet. Version-defensive: EEVEE Next (Blender 4.2+/5.x) needs ray
    tracing on and uses fast-GI AO; EEVEE Legacy (<=4.1) uses GTAO; Cycles uses
    fast-GI. Every attribute is hasattr-guarded so it can never crash a render,
    and AO-off leaves the scene untouched."""
    if not render.get("ao_enabled"):
        return
    distance = float(render.get("ao_distance", 0.2) or 0.2)
    factor = float(render.get("ao_factor", 1.0) or 1.0)
    engine = getattr(scene.render, "engine", "")
    try:
        ee = getattr(scene, "eevee", None)
        if engine.startswith("BLENDER_EEVEE") and ee is not None:
            if hasattr(ee, "use_gtao"):                       # EEVEE Legacy (<= 4.1)
                ee.use_gtao = True
                if hasattr(ee, "gtao_distance"):
                    ee.gtao_distance = distance
                if hasattr(ee, "gtao_factor"):
                    ee.gtao_factor = factor
                log(f"[ao] EEVEE GTAO on — distance={distance} factor={factor}")
                return
            if hasattr(ee, "use_fast_gi"):                    # EEVEE Next (4.2+ / 5.x)
                if hasattr(ee, "use_raytracing"):
                    ee.use_raytracing = True                  # required for fast-GI to do anything
                ee.use_fast_gi = True
                if hasattr(ee, "fast_gi_method"):
                    try:
                        ee.fast_gi_method = "AMBIENT_OCCLUSION_ONLY"
                    except (TypeError, ValueError):
                        pass
                if hasattr(ee, "fast_gi_distance"):
                    ee.fast_gi_distance = distance
                log(f"[ao] EEVEE Next fast-GI AO on — distance={distance}")
                return
        if engine == "CYCLES" and hasattr(scene, "cycles"):
            cy = scene.cycles
            if hasattr(cy, "use_fast_gi"):
                cy.use_fast_gi = True
                if hasattr(cy, "ao_bounces_render"):
                    cy.ao_bounces_render = max(1, round(factor))
                if hasattr(cy, "ao_bounces"):
                    cy.ao_bounces = max(1, round(factor))
                log("[ao] Cycles fast-GI AO on")
                return
        log(f"[ao] ambient occlusion not available for engine {engine}")
    except Exception as exc:
        log(f"[ao] could not enable ambient occlusion: {exc}")


def setup_audio(scene, audio_paths) -> None:
    """Mux one or more source clips' audio into the render via sequencer sound
    strips (one per clip, mixed), while keeping the 3D scene (not the VSE) as
    the image source. Accepts a single path (str) or a list of paths."""
    if isinstance(audio_paths, str):
        audio_paths = [audio_paths] if audio_paths else []
    paths = [p for p in (audio_paths or []) if p]
    if not paths:
        return
    try:
        scene.sequence_editor_create()
        se = scene.sequence_editor
        coll = se.strips if hasattr(se, "strips") else se.sequences
        added = 0
        for p in paths:
            path = _resolve_path(p)
            if not os.path.exists(path) or _is_still_image(path):
                continue
            channel = added + 1
            coll.new_sound(
                name=f"AUTO_AUDIO_{channel}",
                filepath=path,
                channel=channel,
                frame_start=int(scene.frame_start),
            )
            added += 1
            log(f"Audio muxed from: {Path(path).name}")
        if added:
            scene.render.use_sequencer = False  # image comes from the 3D render
            scene.render.ffmpeg.audio_codec = "AAC"
            try:
                scene.render.ffmpeg.audio_bitrate = 192
            except Exception:
                pass
    except Exception as exc:
        log(f"Audio setup skipped: {exc}")


def apply_video_to_material(
    material_name: str,
    video_path: str,
    frame_start: int,
    frame_end: int,
    mapping_mode: str = VIDEO_MAPPING_MODE_EMISSION,
) -> None:
    material = bpy.data.materials.get(material_name)
    if material is None:
        raise RuntimeError(f"Material not found: {material_name}")

    # Ensure the material has a shader node tree. Only touch the deprecated
    # `use_nodes` flag when there's genuinely no node tree (legacy materials) —
    # modern Blender materials already have one, and from Blender 6.0 they always
    # will, so this stays warning-free and forward-compatible.
    if material.node_tree is None:
        material.use_nodes = True

    node_tree = material.node_tree
    nodes = node_tree.nodes
    links = node_tree.links
    output = _material_output_node(nodes)

    _clear_auto_video_nodes(nodes)

    normalized_mode = str(mapping_mode or VIDEO_MAPPING_MODE_EMISSION).upper()

    if normalized_mode == VIDEO_MAPPING_MODE_EMISSION:
        emission = nodes.new(type="ShaderNodeEmission")
        emission.name = "AUTO_VIDEO_EMISSION"
        emission.label = "AUTO_VIDEO_EMISSION"
        emission.location = (output.location.x - 260, output.location.y)

        tex = _create_movie_texture_node(
            nodes=nodes,
            video_path=video_path,
            frame_start=frame_start,
            frame_end=frame_end,
            location=(emission.location.x - 340, emission.location.y),
        )

        emission.inputs["Strength"].default_value = 1.0
        links.new(tex.outputs["Color"], emission.inputs["Color"])
        links.new(emission.outputs["Emission"], output.inputs["Surface"])
        log(f"Applied full-bright emission video to material '{material_name}': {video_path}")
        return

    if normalized_mode != VIDEO_MAPPING_MODE_BASE_COLOR:
        raise RuntimeError(f"Unsupported material mapping mode: {mapping_mode}")

    principled = _principled_node(nodes)

    if principled is None:
        raise RuntimeError(f"No Principled BSDF node found in material: {material_name}")

    tex = _create_movie_texture_node(
        nodes=nodes,
        video_path=video_path,
        frame_start=frame_start,
        frame_end=frame_end,
        location=(principled.location.x - 360, principled.location.y),
    )

    base_color_input = principled.inputs.get("Base Color")
    alpha_input = principled.inputs.get("Alpha")

    if base_color_input is not None:
        base_color_input.default_value = (1.0, 1.0, 1.0, 1.0)
        links.new(tex.outputs["Color"], base_color_input)
    if alpha_input is not None:
        links.new(tex.outputs["Alpha"], alpha_input)

    log(f"Applied base-color video texture to material '{material_name}': {video_path}")


def material_assignments_from_config(config: dict) -> list[dict[str, str]]:
    configured = config.get("material_assignments", [])
    assignments: list[dict[str, str]] = []

    if isinstance(configured, list):
        for item in configured:
            if not isinstance(item, dict):
                continue

            material_name = str(item.get("material_name", "")).strip()
            video_path = str(item.get("video_path", "")).strip()
            mapping_mode = str(item.get("mapping_mode", VIDEO_MAPPING_MODE_EMISSION)).strip().upper()
            if material_name and video_path:
                assignments.append(
                    {
                        "material_name": material_name,
                        "video_path": video_path,
                        "mapping_mode": mapping_mode or VIDEO_MAPPING_MODE_EMISSION,
                    }
                )

    if assignments:
        return assignments

    material_name = str(config.get("target_material", "")).strip()
    video_path = str(config.get("video_path", "")).strip()
    if material_name and video_path:
        return [
            {
                "material_name": material_name,
                "video_path": video_path,
                "mapping_mode": VIDEO_MAPPING_MODE_EMISSION,
            }
        ]

    # Nothing mapped → render the scene exactly as-is (e.g. previewing the bare
    # 3D model before any video is linked).
    log("No video mappings configured — rendering the scene as-is")
    return []


def set_camera(camera_name: str) -> None:
    """Ensure the scene has an active render camera.

    Uses ``camera_name`` when it names a camera object; otherwise keeps an
    already-active camera (a ``.blend`` usually ships one), and failing that
    falls back to the first camera in the scene. Only raises when the scene has
    no camera at all. The fallback matters for imported glTF/FBX scenes: Blender
    imports their cameras but does NOT set one as the active scene camera, so
    without this a ``.glb`` render dies with "Cannot render, no camera"."""
    scene = bpy.context.scene
    if camera_name:
        camera_obj = bpy.data.objects.get(camera_name)
        if camera_obj is not None and camera_obj.type == "CAMERA":
            scene.camera = camera_obj
            log(f"Set active camera: {camera_name}")
            return
        log(f"WARNING: camera '{camera_name}' not found — falling back to another camera")

    if scene.camera is not None and getattr(scene.camera, "type", "") == "CAMERA":
        log(f"Using the scene's active camera: {scene.camera.name}")
        return

    cams = [o for o in scene.objects if o.type == "CAMERA"]
    if not cams:
        cams = [o for o in bpy.data.objects if o.type == "CAMERA"]
        if cams:   # imported but not linked to the active scene — link it so it renders
            try:
                scene.collection.objects.link(cams[0])
            except Exception:
                pass
    if not cams:
        raise RuntimeError("No camera in the scene to render from")
    scene.camera = cams[0]
    log(f"Set active camera (fallback): {cams[0].name}")


def configure_render(config: dict) -> None:
    scene = bpy.context.scene
    render = config["render"]

    requested_engine = str(render.get("engine", "CYCLES")).upper()
    supported_engines = {
        item.identifier for item in scene.render.bl_rna.properties["engine"].enum_items
    }
    # Blender versions differ between BLENDER_EEVEE and BLENDER_EEVEE_NEXT.
    if requested_engine == "BLENDER_EEVEE_NEXT" and "BLENDER_EEVEE" in supported_engines:
        requested_engine = "BLENDER_EEVEE"
    elif requested_engine == "BLENDER_EEVEE" and "BLENDER_EEVEE_NEXT" in supported_engines:
        requested_engine = "BLENDER_EEVEE_NEXT"
    if requested_engine not in supported_engines:
        requested_engine = "CYCLES" if "CYCLES" in supported_engines else next(iter(supported_engines))

    scene.render.engine = requested_engine
    scene.render.resolution_x = int(render["width"])
    scene.render.resolution_y = int(render["height"])
    scene.render.fps = int(render["fps"])
    scene.frame_start = int(render["frame_start"])
    scene.frame_end = int(render["frame_end"])
    scene.frame_step = max(1, int(render.get("frame_step", 1)))

    output_format = str(render.get("output_format", "MPEG4")).upper()
    codec = str(render.get("codec", "H264")).upper()
    if output_format in {"MPEG4", "QUICKTIME"}:
        # Assemble the movie ourselves: render a lossless PNG sequence, then encode
        # it with the bundled ffmpeg. Richer than Blender's internal writer
        # (H.265 / ProRes, proper rec.709 tagging), consistent with the C4D + web
        # backends, and immune to Blender's version-fragile FFMPEG-format setter
        # (the Python setter rejects 'FFMPEG' on 5.x and the imtype byte-poke
        # silently stopped taking on recent builds). Prepare/export mode renders
        # nothing here, so it just leaves the .blend on a PNG sequence.
        global _MOVIE_MUX
        scene.render.image_settings.file_format = "PNG"
        if str(config.get("prepared_blend_path", "")).strip():
            log("Movie output: the prepared .blend will render a PNG sequence — "
                "assemble it to a movie with ffmpeg on the farm.")
        else:
            _MOVIE_MUX = {
                "format": output_format,
                "codec": str(render.get("video_codec", "")).strip().upper() or codec,
                "fps": int(render["fps"]),
                "quality": str(render.get("video_quality", "HIGH")).strip().upper() or "HIGH",
                "seq_dir": Path(tempfile.mkdtemp(prefix="blender-movieseq-")),
            }
            log("Movie output: rendering a PNG sequence and encoding to the movie "
                "with the bundled ffmpeg.")
    elif output_format in {"PNG", "OPEN_EXR"}:
        scene.render.image_settings.file_format = output_format
    else:
        raise RuntimeError(f"Unsupported output format: {output_format}")

    # Transparent background (alpha) — needs an RGBA-capable format.
    if bool(render.get("film_transparent", False)):
        try:
            scene.render.film_transparent = True
            scene.render.image_settings.color_mode = "RGBA"
            log("Transparent background enabled (RGBA)")
        except Exception as exc:
            log(f"Could not enable transparency: {exc}")

    # Render scale.
    try:
        scene.render.resolution_percentage = max(1, min(100, int(render.get("resolution_percentage", 100))))
    except Exception:
        scene.render.resolution_percentage = 100

    # Never let Simplify downscale our video textures — the screens must show
    # the clip at full resolution regardless of the scene's Simplify setting.
    try:
        if hasattr(scene, "cycles"):
            scene.cycles.texture_limit = "OFF"
            scene.cycles.texture_limit_render = "OFF"
    except Exception as exc:
        log(f"Could not clear texture limit: {exc}")

    if scene.render.engine == "CYCLES" and hasattr(scene, "cycles"):
        scene.cycles.samples = int(render.get("samples", 64))
        try:
            scene.cycles.use_denoising = bool(render.get("use_denoise", True))
        except Exception as exc:
            log(f"Could not set denoising: {exc}")
        _apply_cycles_device(scene, str(render.get("device", "AUTO")).upper())

    _apply_ambient_occlusion(scene, render, log)

    # Color management controls for predictable output between runs.
    scene.view_settings.view_transform = str(render.get("color_view_transform", "Filmic"))
    scene.view_settings.look = str(render.get("color_look", "None"))
    scene.view_settings.exposure = float(render.get("color_exposure", 0.0))
    scene.view_settings.gamma = float(render.get("color_gamma", 1.0))

    # Burn-in overlay: stamp the clip name/version, frame, camera and date onto
    # every frame so versioned previz reviews are unambiguous.
    if render.get("burn_in"):
        r = scene.render
        r.use_stamp = True
        r.use_stamp_note = True
        r.use_stamp_frame = True
        r.use_stamp_date = True
        r.use_stamp_camera = True
        r.use_stamp_time = False
        r.use_stamp_render_time = False
        r.use_stamp_scene = False
        r.use_stamp_filename = False
        r.use_stamp_hostname = False
        r.use_stamp_lens = False
        r.use_stamp_frame_range = False
        r.use_stamp_memory = False
        r.use_stamp_marker = False
        r.use_stamp_sequencer_strip = False
        clips = ", ".join(
            Path(a.get("video_path", "")).stem
            for a in config.get("material_assignments", []) if a.get("video_path"))
        r.stamp_note_text = clips[:128]
        r.stamp_font_size = max(12, int(render["height"]) // 40)
        r.stamp_foreground = (1.0, 1.0, 1.0, 0.9)
        r.stamp_background = (0.0, 0.0, 0.0, 0.45)

    original_output_path = Path(config["output_path"]).expanduser().resolve()
    output_format = str(render.get("output_format", "MPEG4")).upper()
    is_video = output_format in {"MPEG4", "QUICKTIME"}

    if config.get("use_deadline", False):
        temp_dir = _render_temp_dir()
        if is_video:
            actual_output_path = temp_dir / original_output_path.name
        else:
            actual_output_path = temp_dir / "render_output"
            actual_output_path.mkdir(parents=True, exist_ok=True)
    else:
        actual_output_path = original_output_path

    if _MOVIE_MUX is not None:
        # Movie fallback: render the PNG sequence to a temp dir; main() muxes it
        # to the real movie path (actual_output_path) once the render completes.
        _MOVIE_MUX["target"] = str(actual_output_path)
        scene.render.filepath = str(_MOVIE_MUX["seq_dir"]) + os.sep
    elif output_format in {"PNG", "OPEN_EXR"}:
        actual_output_path.mkdir(parents=True, exist_ok=True)
        scene.render.filepath = str(actual_output_path) + os.sep
    else:
        actual_output_path.parent.mkdir(parents=True, exist_ok=True)
        scene.render.filepath = str(actual_output_path)

    log(
        "Render config: "
        f"{scene.render.resolution_x}x{scene.render.resolution_y}, "
        f"fps={scene.render.fps}, "
        f"frames={scene.frame_start}-{scene.frame_end}, "
        f"engine={scene.render.engine}"
    )
    log(f"Output path: {actual_output_path}")


def setup_live_preview(scene, preview_path: str) -> None:
    """Save each rendered frame as a JPEG to ``preview_path`` so the desktop app
    can show a live preview, independent of the final output format.

    During an FFMPEG movie render Blender 5.x locks ``image_settings.file_format``
    to ('FFMPEG') in the RNA setter, so we reuse the same ctypes byte-poke the
    rest of this worker relies on: we discover the JPEG enum byte by scanning,
    then flip to it only for the still save and restore the movie format right
    after. Writes go to a temp sibling + atomic rename so the reader (the app)
    never sees a half-written frame.
    """
    if not preview_path:
        return

    # Discover the JPEG ``imtype`` byte from a throwaway scene whose image
    # settings are *not* movie-locked (the active scene's file_format enum is
    # restricted to ('FFMPEG') during a movie render, so we can't read it there).
    jpeg_val = None
    try:
        probe = bpy.data.scenes.new("RMP_PREVIEW_FMT")
        probe.render.image_settings.file_format = "JPEG"
        jpeg_val = ctypes.c_int8.from_address(probe.render.image_settings.as_pointer()).value
    except Exception as exc:
        log(f"Live preview unavailable: {exc}")
        return
    if jpeg_val is None:
        return

    ptr = scene.render.image_settings.as_pointer()
    try:
        os.makedirs(os.path.dirname(preview_path) or ".", exist_ok=True)
    except Exception:
        pass

    def _save_preview(s, *args) -> None:
        # save_render reads the format from the C-level imtype byte, so poking
        # it to JPEG lets us save a still even while the RNA enum is FFMPEG.
        try:
            img = bpy.data.images.get("Render Result")
            if img is None:
                return
            cur = ctypes.c_int8.from_address(ptr).value
            try:
                ctypes.c_int8.from_address(ptr).value = jpeg_val
                tmp = preview_path + ".tmp"
                img.save_render(filepath=tmp, scene=s)
                os.replace(tmp, preview_path)
            finally:
                ctypes.c_int8.from_address(ptr).value = cur
        except Exception as exc:
            log(f"Preview frame skipped: {exc}")

    bpy.app.handlers.render_post.append(_save_preview)
    log(f"Live preview enabled -> {preview_path}")


def main() -> None:
    config_path = parse_config_path()
    config = json.loads(config_path.read_text())
    assignments = material_assignments_from_config(config)

    _bv = tuple(bpy.app.version[:2])
    if _bv < (4, 0) or _bv >= (6, 0):
        log(f"[warn] Blender {'.'.join(map(str, bpy.app.version))} is outside the "
            "tested range (4.0–5.x); movie output / live preview may misbehave.")

    if bool(config.get("safe_mode", True)) and not config.get("use_deadline", False):
        scene_path = Path(_resolve_path(config["scene_path"]))
        if not scene_path.exists():
            raise RuntimeError("Safe mode check failed: scene path does not exist")
        for assignment in assignments:
            video_path = Path(_resolve_path(assignment["video_path"]))
            if not video_path.exists():
                raise RuntimeError(f"Safe mode check failed: video path does not exist: {video_path}")
            allowed = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"} | IMAGE_EXTENSIONS
            if video_path.suffix.lower() not in allowed:
                raise RuntimeError(f"Safe mode check failed: unsupported media extension: {video_path}")

    load_scene(config["scene_path"])
    # A .blend is the artist's full scene (its own lighting); only auto-light
    # IMPORTED scenes (glTF/FBX/…), which usually ship none → black in Blender.
    if Path(str(config["scene_path"])).suffix.lower() != ".blend":
        ensure_lighting(config)
    target_cam = str(config.get("target_camera", "")).strip()
    set_camera(target_cam)   # always ensure an active camera (falls back if needed)
    for assignment in assignments:
        apply_video_to_material(
            material_name=assignment["material_name"],
            video_path=_resolve_path(assignment["video_path"]),
            frame_start=int(config["render"]["frame_start"]),
            frame_end=int(config["render"]["frame_end"]),
            mapping_mode=assignment.get("mapping_mode", VIDEO_MAPPING_MODE_EMISSION),
        )
    configure_render(config)

    # On a Deadline farm node, Deadline substitutes the task's frame range as two
    # trailing args (<STARTFRAME> <ENDFRAME>). The video mapping above already used
    # the FULL range (so the clip plays over the whole timeline); here we narrow the
    # RENDERED range to this task's chunk so nodes split the work instead of each
    # re-rendering everything. Absent (local render / single task) → full range.
    _chunk = _parse_task_frames()
    if _chunk:
        bpy.context.scene.frame_start, bpy.context.scene.frame_end = _chunk
        log(f"Deadline task frame range: {_chunk[0]}-{_chunk[1]}")

    # Prepared-.blend export: the scene now has the video mapping and render
    # settings baked in, so save it as a standalone .blend that any render farm
    # (Deadline, BlendFarm, cloud, or plain Blender) can render — then stop.
    prepared = str(config.get("prepared_blend_path", "")).strip()
    if prepared:
        Path(prepared).expanduser().parent.mkdir(parents=True, exist_ok=True)
        if config.get("pack_blend", False):
            try:
                bpy.ops.file.pack_all()
                log("Packed external files into the .blend")
            except Exception as exc:
                log(f"Warning: could not pack files: {exc}")
        else:
            try:
                bpy.ops.file.make_paths_relative()
            except Exception:
                pass
        bpy.ops.wm.save_as_mainfile(filepath=str(Path(prepared).expanduser()))
        log(f"Prepared .blend saved: {prepared}")
        return

    setup_audio(
        bpy.context.scene,
        config.get("audio_paths") or str(config.get("audio_path", "")).strip(),
    )
    setup_live_preview(bpy.context.scene, str(config.get("preview_path", "")).strip())

    # Single-frame preview: video mapping above was set up over the FULL frame
    # range (so the clip is in sync), but here we collapse the scene render range
    # to just the requested frame so only that one frame is rendered.
    preview_frame = int(config.get("preview_frame", 0) or 0)
    if preview_frame > 0:
        scene = bpy.context.scene
        scene.frame_start = preview_frame
        scene.frame_end = preview_frame
        scene.frame_set(preview_frame)
        log(f"Single-frame preview render at frame {preview_frame}")

    log("Starting headless animation render")
    bpy.ops.render.render(animation=True)

    # Movie fallback: Blender just rendered a PNG sequence because its FFMPEG
    # writer was unavailable. Assemble it into the requested movie (with audio)
    # using the bundled ffmpeg, then drop the temp sequence.
    if _MOVIE_MUX is not None:
        seq_dir = _MOVIE_MUX["seq_dir"]
        frames = sorted(Path(seq_dir).glob("*.png"))
        if not frames:
            raise RuntimeError("Movie fallback: Blender wrote no frames to assemble.")
        _mux_sequence_to_movie(
            _worker_ffmpeg(config), str(seq_dir), _MOVIE_MUX["fps"],
            _MOVIE_MUX["target"], _MOVIE_MUX["format"], _MOVIE_MUX["codec"],
            _MOVIE_MUX["quality"], audio_paths=config.get("audio_paths") or [])
        shutil.rmtree(seq_dir, ignore_errors=True)

    log("Render finished successfully")

    original_output_path = Path(config["output_path"]).expanduser().resolve()
    output_format = str(config["render"].get("output_format", "MPEG4")).upper()
    is_video = output_format in {"MPEG4", "QUICKTIME"}

    if config.get("use_deadline", False):
        temp_dir = _render_temp_dir()
        if is_video:
            actual_output_path = temp_dir / original_output_path.name
        else:
            actual_output_path = temp_dir / "render_output"
            actual_output_path.mkdir(parents=True, exist_ok=True)  # match the setup block
    else:
        actual_output_path = original_output_path

    # Rename video files to match the exact output path if Blender appended a frame range
    try:
        if is_video:
            expected_path = actual_output_path
            if not expected_path.exists():
                parent_dir = expected_path.parent
                stem = expected_path.stem
                ext = expected_path.suffix
                fs = int(config["render"]["frame_start"])
                fe = int(config["render"]["frame_end"])

                possible_names = [
                    f"{stem}{fs:04d}-{fe:04d}{ext}",
                    f"{stem}_{fs:04d}-{fe:04d}{ext}",
                    f"{stem}{fs:04d}_{fe:04d}{ext}",
                    f"{stem}_{fs:04d}_{fe:04d}{ext}",
                ]

                for name in possible_names:
                    p = parent_dir / name
                    if p.exists():
                        log(f"Renaming rendered video from {p.name} to {expected_path.name}")
                        p.rename(expected_path)
                        break
    except Exception as e:
        log(f"Warning: failed to rename output video file: {e}")

    # Copy files from actual_output_path to original_output_path
    if config.get("use_deadline", False):
        try:
            log(f"Copying final render from local task directory to: {original_output_path}")
            if is_video:
                original_output_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(actual_output_path, original_output_path)
            else:
                original_output_path.mkdir(parents=True, exist_ok=True)
                for f in actual_output_path.glob("*"):
                    if f.is_file():
                        shutil.copy2(f, original_output_path / f.name)
            log("Copy completed successfully")
        except Exception as e:
            log(f"ERROR: Failed to copy final render to destination '{original_output_path}': {e}")
            raise e
        finally:
            temp_dir = _render_temp_dir()
            try:
                log(f"Cleaning up local temp directory: {temp_dir}")
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception as e:
                log(f"Warning: failed to clean up local temp directory {temp_dir}: {e}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
    finally:
        # Guarantee the local temp dir is removed even if a render fails before
        # reaching the copy-out step (which has its own cleanup on the happy path).
        _cleanup_render_temp()
