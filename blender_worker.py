from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import bpy

VIDEO_MAPPING_MODE_EMISSION = "EMISSION_FULL_BRIGHT"
VIDEO_MAPPING_MODE_BASE_COLOR = "BASE_COLOR_ALPHA"


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
    camera_obj = bpy.data.objects.get(camera_name)
    if camera_obj is None or camera_obj.type != "CAMERA":
        raise RuntimeError(f"Camera not found (or not camera type): {camera_name}")

    bpy.context.scene.camera = camera_obj
    log(f"Set active camera: {camera_name}")


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
    supported_file_formats = {
        item.identifier for item in scene.render.image_settings.bl_rna.properties["file_format"].enum_items
    }

    if output_format in {"MPEG4", "QUICKTIME"}:
        if "FFMPEG" not in supported_file_formats:
            raise RuntimeError(
                "FFMPEG movie output unavailable in this Blender runtime. "
                f"Requested profile={output_format}/{codec}. "
                f"Supported image file formats: {sorted(supported_file_formats)}"
            )
        try:
            scene.render.image_settings.file_format = "FFMPEG"
        except (TypeError, AttributeError):
            # Blender 5.x bug: the Python property setter for file_format rejects
            # 'FFMPEG' even when the binary supports it and enum_items lists it.
            # Only apply the ctypes workaround on affected builds.
            blender_version = tuple(bpy.app.version[:2])
            if blender_version >= (4, 0):
                try:
                    _fmt = scene.render.image_settings
                    ctypes.c_int8.from_address(_fmt.as_pointer()).value = 24
                    scene.render.image_settings.file_format = "FFMPEG"
                except Exception:
                    pass  # Fall through to the outer error handler
        except Exception as exc:
            current_supported_formats = {
                item.identifier
                for item in scene.render.image_settings.bl_rna.properties["file_format"].enum_items
            }
            raise RuntimeError(
                "Blender rejected movie output format FFMPEG at runtime. "
                f"Requested profile={output_format}/{codec}. "
                f"Current file_format={scene.render.image_settings.file_format}. "
                f"Supported image file formats now: {sorted(current_supported_formats)}. "
                f"Original error: {exc}"
            ) from exc
        scene.render.ffmpeg.format = output_format
        # Optional codec override (e.g. H265); blank keeps the profile default.
        codec_override = str(render.get("video_codec", "")).strip().upper()
        scene.render.ffmpeg.codec = codec_override or codec
        crf = str(render.get("video_quality", "HIGH")).strip().upper() or "HIGH"
        try:
            scene.render.ffmpeg.constant_rate_factor = crf
        except Exception:
            scene.render.ffmpeg.constant_rate_factor = "HIGH"
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

    # Color management controls for predictable output between runs.
    scene.view_settings.view_transform = str(render.get("color_view_transform", "Filmic"))
    scene.view_settings.look = str(render.get("color_look", "None"))
    scene.view_settings.exposure = float(render.get("color_exposure", 0.0))
    scene.view_settings.gamma = float(render.get("color_gamma", 1.0))

    original_output_path = Path(config["output_path"]).expanduser().resolve()
    output_format = str(render.get("output_format", "MPEG4")).upper()
    is_video = output_format in {"MPEG4", "QUICKTIME"}

    if config.get("use_deadline", False):
        temp_dir = Path(tempfile.gettempdir()) / f"blender_render_{os.getpid()}"
        if is_video:
            actual_output_path = temp_dir / original_output_path.name
        else:
            actual_output_path = temp_dir / "render_output"
            actual_output_path.mkdir(parents=True, exist_ok=True)
    else:
        actual_output_path = original_output_path

    if output_format in {"PNG", "OPEN_EXR"}:
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
    target_cam = str(config.get("target_camera", "")).strip()
    if target_cam:
        set_camera(target_cam)
    for assignment in assignments:
        apply_video_to_material(
            material_name=assignment["material_name"],
            video_path=_resolve_path(assignment["video_path"]),
            frame_start=int(config["render"]["frame_start"]),
            frame_end=int(config["render"]["frame_end"]),
            mapping_mode=assignment.get("mapping_mode", VIDEO_MAPPING_MODE_EMISSION),
        )
    configure_render(config)

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
    log("Render finished successfully")

    original_output_path = Path(config["output_path"]).expanduser().resolve()
    output_format = str(config["render"].get("output_format", "MPEG4")).upper()
    is_video = output_format in {"MPEG4", "QUICKTIME"}

    if config.get("use_deadline", False):
        temp_dir = Path(tempfile.gettempdir()) / f"blender_render_{os.getpid()}"
        if is_video:
            actual_output_path = temp_dir / original_output_path.name
        else:
            actual_output_path = temp_dir / "render_output"
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
            temp_dir = Path(tempfile.gettempdir()) / f"blender_render_{os.getpid()}"
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
