"""Cinema 4D headless render worker — run under c4dpy.

Reads a JobConfig JSON (the same shape the Blender worker consumes) and renders
the mapped scene with Redshift, injecting each assignment's video into the
target material's Redshift emission. Frames are written to <output_path>/####.png
(the app detects success by the produced files, since c4dpy can throw a harmless
exception during interpreter teardown).

    printf '1\\n' | c4dpy c4d_worker.py /path/to/config.json
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

import c4d
import maxon
from c4d import documents

RS_SPACE = "com.redshift3d.redshift4c4d.class.nodespace"
STD = "com.redshift3d.redshift4c4d.nodes.core.standardmaterial"
TEX = "com.redshift3d.redshift4c4d.nodes.core.texturesampler"


def log(message: str) -> None:
    print(f"[c4d-worker] {message}")
    sys.stdout.flush()


def _find_object(doc, name):
    def walk(obj):
        while obj:
            if obj.GetName() == name:
                return obj
            found = walk(obj.GetDown())
            if found:
                return found
            obj = obj.GetNext()
        return None
    return walk(doc.GetFirstObject())


def _find_path_port(tex):
    tex0 = tex.GetInputs().FindChild(TEX + ".tex0")
    for sub in tex0.GetChildren():
        if str(sub.GetId()).split(".")[-1] == "path":
            return sub
    return None


def _inject_emission_texture(mat):
    """Add a Redshift texture node wired into the material's emission and return
    (graph, path_port) so the per-frame still can be swapped in. Redshift can't
    read .mp4, so we feed it PNG stills extracted with ffmpeg."""
    nm = mat.GetNodeMaterialReference()
    if nm is None or not nm.HasSpace(maxon.Id(RS_SPACE)):
        log(f"  '{mat.GetName()}' is not a Redshift node material — skipped")
        return None, None
    graph = nm.GetGraph(maxon.Id(RS_SPACE))
    transaction = graph.BeginTransaction()
    try:
        stds = maxon.GraphModelHelper.FindNodesByAssetId(graph, maxon.Id(STD), True)
        if not stds:
            log(f"  no StandardMaterial node in '{mat.GetName()}'")
            transaction.Commit()
            return None, None
        std = stds[0]
        tex = graph.AddChild(maxon.Id(""), maxon.Id(TEX), maxon.DataDictionary())
        path_port = _find_path_port(tex)
        tex.GetOutputs().FindChild(TEX + ".outcolor").Connect(
            std.GetInputs().FindChild(STD + ".emission_color"))
        # Full-bright screen: emission only. Kill diffuse + reflection so the
        # screen shows *exactly* the clip (a black video frame → black screen),
        # unaffected by scene lighting — matching Blender's EMISSION_FULL_BRIGHT.
        for leaf, val in (("emission_weight", 1.0), ("base_color_weight", 0.0),
                          ("refl_weight", 0.0), ("coat_weight", 0.0), ("sheen_weight", 0.0)):
            port = std.GetInputs().FindChild(STD + "." + leaf)
            if port:
                port.SetPortValue(maxon.Float64(val))
        transaction.Commit()
        return graph, path_port
    except Exception as exc:
        log(f"  mapping error on '{mat.GetName()}': {exc}")
        try:
            transaction.Commit()
        except Exception:
            pass
        return None, None


def _set_still(graph, path_port, image_path) -> None:
    transaction = graph.BeginTransaction()
    try:
        path_port.SetPortValue(maxon.Url(image_path))
        transaction.Commit()
    except Exception:
        transaction.Commit()


def _inject_emission_sequence(mat, first_frame_path, fstart, fend, fps) -> bool:
    """Bake the clip into the material as a Redshift image-sequence emission so
    the scene renders the right frame *natively* — letting the licensed Cinema 4D
    Commandline renderer drive it on the farm with no Python/c4dpy per node."""
    nm = mat.GetNodeMaterialReference()
    if nm is None or not nm.HasSpace(maxon.Id(RS_SPACE)):
        log(f"  '{mat.GetName()}' is not a Redshift node material — skipped")
        return False
    graph = nm.GetGraph(maxon.Id(RS_SPACE))
    tr = graph.BeginTransaction()
    try:
        stds = maxon.GraphModelHelper.FindNodesByAssetId(graph, maxon.Id(STD), True)
        if not stds:
            tr.Commit()
            return False
        std = stds[0]
        tex = graph.AddChild(maxon.Id(""), maxon.Id(TEX), maxon.DataDictionary())
        tex.GetOutputs().FindChild(TEX + ".outcolor").Connect(
            std.GetInputs().FindChild(STD + ".emission_color"))
        for leaf, val in (("emission_weight", 1.0), ("base_color_weight", 0.0),
                          ("refl_weight", 0.0), ("coat_weight", 0.0), ("sheen_weight", 0.0)):
            p = std.GetInputs().FindChild(STD + "." + leaf)
            if p:
                p.SetPortValue(maxon.Float64(val))
        tex0 = tex.GetInputs().FindChild(TEX + ".tex0")

        def setsub(leaf, value):
            for sub in tex0.GetChildren():
                if str(sub.GetId()).split(".")[-1] == leaf:
                    try:
                        sub.SetPortValue(value)
                        return True
                    except Exception as exc:
                        log(f"   seq port '{leaf}' err: {exc}")
            return False

        setsub("path", maxon.Url(first_frame_path))
        setsub("animation", maxon.Bool(True))
        setsub("framestart", maxon.Int32(int(fstart)))
        setsub("frameend", maxon.Int32(int(fend)))
        setsub("framerate", maxon.Float64(float(fps)))
        tr.Commit()
        return True
    except Exception as exc:
        log(f"  sequence mapping error on '{mat.GetName()}': {exc}")
        try:
            tr.Commit()
        except Exception:
            pass
        return False


def prepare_scene(cfg) -> None:
    """Bake the video mapping + render settings into a standalone .c4d that the
    licensed Cinema 4D Commandline renderer can render on the farm. Frames are
    pre-extracted to an image sequence next to the prepared scene."""
    render = cfg.get("render", {})
    doc = documents.LoadDocument(cfg["scene_path"],
                                 c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS)
    if doc is None:
        raise RuntimeError(f"Could not load scene: {cfg['scene_path']}")
    documents.InsertBaseDocument(doc)
    documents.SetActiveDocument(doc)
    fps = int(render.get("fps", 30))
    doc.SetFps(fps)

    ffmpeg = str(cfg.get("ffmpeg_path", "")).strip() or shutil.which("ffmpeg") or "ffmpeg"
    prepared = cfg["prepare_c4d_path"]
    seq_dir = cfg.get("sequence_dir") or os.path.join(os.path.dirname(prepared), "seq")
    os.makedirs(seq_dir, exist_ok=True)
    fstart = int(render.get("frame_start", 1))
    fend = int(render.get("frame_end", 1))

    mats = {m.GetName(): m for m in doc.GetMaterials()}
    for i, a in enumerate(cfg.get("material_assignments", [])):
        name, vid = a.get("material_name"), a.get("video_path")
        if name not in mats or not vid:
            continue
        log(f"Baking '{name}' <- {os.path.basename(vid)} ({fstart}-{fend})")
        first = None
        for f in range(fstart, fend + 1):
            out_png = os.path.join(seq_dir, f"clip{i}_{f:04d}.png")
            if _extract_frame(ffmpeg, vid, f, fps, out_png):
                first = first or out_png
        if first:
            # Store the sequence path *relative* to the prepared scene so it
            # resolves on any node regardless of where the repo is mounted.
            rel = os.path.relpath(first, os.path.dirname(prepared))
            _inject_emission_sequence(mats[name], rel, fstart, fend, fps)

    cam_name = str(cfg.get("target_camera", "")).strip()
    if cam_name:
        cam = _find_object(doc, cam_name)
        bd = doc.GetActiveBaseDraw()
        if cam and bd:
            bd.SetSceneCamera(cam)

    rd = doc.GetActiveRenderData()
    _apply_redshift_quality(rd, render)
    width = int(render.get("width", 1920))
    height = int(render.get("height", 1080))
    pct = max(1, min(100, int(render.get("resolution_percentage", 100))))
    rd[c4d.RDATA_XRES] = float(max(1, width * pct // 100))
    rd[c4d.RDATA_YRES] = float(max(1, height * pct // 100))
    rd[c4d.RDATA_FRAMEFROM] = c4d.BaseTime(fstart, fps)
    rd[c4d.RDATA_FRAMETO] = c4d.BaseTime(fend, fps)

    os.makedirs(os.path.dirname(prepared) or ".", exist_ok=True)
    if not documents.SaveDocument(doc, prepared, c4d.SAVEDOCUMENTFLAGS_DONTADDTORECENTLIST,
                                  c4d.FORMAT_C4DEXPORT):
        raise RuntimeError(f"Failed to save prepared scene: {prepared}")
    log(f"Prepared scene written: {prepared}")
    log("Render finished successfully")


def _set_vp(vp, const_name: str, value) -> None:
    """Set a Redshift video-post parameter by constant name, if present."""
    if hasattr(c4d, const_name):
        try:
            vp[getattr(c4d, const_name)] = value
        except Exception:
            pass


def _apply_redshift_quality(rd, render: dict) -> None:
    """Drive the Redshift video-post render-speed levers from the app's render
    settings, so the panel controls are real (not cosmetic). Every value here
    trades render time against quality."""
    samples = int(render.get("samples", 0) or 0)
    vp = rd.GetFirstVideoPost()
    while vp:
        if vp.GetType() == 1036219:  # Redshift
            try:
                if samples > 0:
                    _set_vp(vp, "REDSHIFT_RENDERER_UNIFIED_MAX_SAMPLES", int(samples))
                rs_min = int(render.get("rs_min_samples", 4) or 1)
                _set_vp(vp, "REDSHIFT_RENDERER_UNIFIED_MIN_SAMPLES",
                        min(rs_min, samples) if samples > 0 else rs_min)
                _set_vp(vp, "REDSHIFT_RENDERER_UNIFIED_ADAPTIVE_ERROR_THRESHOLD",
                        float(render.get("rs_threshold", 0.01)))
                _set_vp(vp, "REDSHIFT_RENDERER_DENOISE_ENABLED",
                        1 if render.get("use_denoise", True) else 0)
                gi_on = bool(render.get("rs_gi_enabled", True))
                _set_vp(vp, "REDSHIFT_RENDERER_GI_ENABLED", 1 if gi_on else 0)
                if gi_on:
                    _set_vp(vp, "REDSHIFT_RENDERER_NUM_GI_BOUNCES", int(render.get("rs_gi_bounces", 3)))
                _set_vp(vp, "REDSHIFT_RENDERER_MAX_TRACE_DEPTH_COMBINED", int(render.get("rs_ray_depth", 6)))
                log(f"Redshift: samples={samples}/{rs_min}, threshold={render.get('rs_threshold', 0.01)}, "
                    f"denoise={'on' if render.get('use_denoise', True) else 'off'}, "
                    f"GI={'on' if gi_on else 'off'}({render.get('rs_gi_bounces', 3)}), depth={render.get('rs_ray_depth', 6)}")
            except Exception as exc:
                log(f"  redshift quality warning: {exc}")
            break
        vp = vp.GetNext()


def _mux_movie(ffmpeg: str, frames_dir: str, fps: int, out_file: str,
               out_fmt: str, codec: str, quality: str) -> bool:
    """Assemble the rendered PNG frames into a movie with the chosen codec/quality."""
    crf = {"LOSSLESS": "0", "HIGH": "18", "MEDIUM": "23", "LOW": "28", "LOWEST": "32"}
    pattern = os.path.join(frames_dir, "%06d.png")
    os.makedirs(os.path.dirname(out_file) or ".", exist_ok=True)
    # Force even dimensions — H.264/H.265 with yuv420p (and ProRes) reject odd
    # width/height, which a fractional render scale can produce.
    even = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
    cmd = [ffmpeg, "-y", "-framerate", str(fps or 30), "-i", pattern, "-vf", even]
    if str(codec).upper() == "PRORES" or out_fmt == "QUICKTIME":
        cmd += ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"]
    else:
        cmd += ["-c:v", "libx265" if str(codec).upper() == "H265" else "libx264",
                "-crf", crf.get(str(quality).upper(), "18"), "-pix_fmt", "yuv420p"]
    cmd += [out_file]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        ok = os.path.exists(out_file) and os.path.getsize(out_file) > 0
        if not ok:
            log(f"  ffmpeg mux failed: {r.stderr.strip()[-400:]}")
        return ok
    except Exception as exc:
        log(f"  ffmpeg mux error: {exc}")
        return False


def _extract_frame(ffmpeg: str, video: str, frame: int, fps: int, out_png: str) -> bool:
    """Pull one frame out of the video to a PNG (fast input-seek by time)."""
    t = max(0, frame - 1) / float(fps or 30)
    cmd = [ffmpeg, "-y", "-ss", f"{t:.4f}", "-i", video, "-frames:v", "1", out_png]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
        return os.path.exists(out_png) and os.path.getsize(out_png) > 0
    except Exception as exc:
        log(f"  ffmpeg extract failed: {exc}")
        return False


def main() -> None:
    if len(sys.argv) < 2:
        raise RuntimeError("Missing config path argument")
    cfg = json.loads(open(sys.argv[1], encoding="utf-8").read())
    render = cfg.get("render", {})
    scene = cfg["scene_path"]

    # Prepare mode: bake the mapping into a .c4d for the licensed Commandline
    # renderer (used by the Deadline farm path) instead of rendering here.
    if str(cfg.get("prepare_c4d_path", "")).strip():
        prepare_scene(cfg)
        return

    doc = documents.LoadDocument(scene, c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS)
    if doc is None:
        raise RuntimeError(f"Could not load scene: {scene}")
    documents.InsertBaseDocument(doc)
    documents.SetActiveDocument(doc)

    fps = int(render.get("fps", 30))
    doc.SetFps(fps)

    ffmpeg = str(cfg.get("ffmpeg_path", "")).strip() or shutil.which("ffmpeg") or "ffmpeg"
    tmp_dir = tempfile.mkdtemp(prefix="c4d_frames_")

    # ── Map videos onto materials (texture wired to emission; stills swapped
    #    per frame because Redshift can't read .mp4 directly) ───────────────
    mats = {m.GetName(): m for m in doc.GetMaterials()}
    mapped = []  # (graph, path_port, video_path, tag)
    for i, a in enumerate(cfg.get("material_assignments", [])):
        name, vid = a.get("material_name"), a.get("video_path")
        if name in mats and vid:
            log(f"Mapping '{name}' <- {os.path.basename(vid)}")
            graph, port = _inject_emission_texture(mats[name])
            if graph is not None and port is not None:
                mapped.append((graph, port, vid, i))

    # ── Camera ───────────────────────────────────────────────────────────
    cam_name = str(cfg.get("target_camera", "")).strip()
    if cam_name:
        cam = _find_object(doc, cam_name)
        bd = doc.GetActiveBaseDraw()
        if cam and bd:
            bd.SetSceneCamera(cam)
            log(f"Camera: {cam_name}")

    # ── Render settings ──────────────────────────────────────────────────
    rd = doc.GetActiveRenderData()
    # Engine: the app's C4D path is Redshift-only, but honour the field anyway.
    engine_ids = {"STANDARD": 0, "PHYSICAL": 1023342, "REDSHIFT": 1036219}
    eng = str(render.get("engine", "")).strip().upper()
    if eng in engine_ids:
        rd[c4d.RDATA_RENDERENGINE] = engine_ids[eng]
        log(f"Render engine: {eng.title()}")
    width = int(render.get("width", 1920))
    height = int(render.get("height", 1080))
    pct = max(1, min(100, int(render.get("resolution_percentage", 100))))
    xr = max(1, width * pct // 100)
    yr = max(1, height * pct // 100)
    rd[c4d.RDATA_XRES] = float(xr)
    rd[c4d.RDATA_YRES] = float(yr)

    _apply_redshift_quality(rd, render)

    fstart = int(render.get("frame_start", 1))
    fend = int(render.get("frame_end", 1))
    step = max(1, int(render.get("frame_step", 1)))
    preview_frame = int(cfg.get("preview_frame", 0) or 0)
    # Deadline passes this task's frame range as extra argv (config, start, end).
    if len(sys.argv) >= 4 and sys.argv[2].lstrip("-").isdigit() and sys.argv[3].lstrip("-").isdigit():
        fstart, fend, step, preview_frame = int(sys.argv[2]), int(sys.argv[3]), 1, 0
        log(f"Frame range override from farm: {fstart}-{fend}")
    frames = [preview_frame] if preview_frame > 0 else list(range(fstart, fend + 1, step))

    # Output: a movie profile assembles a film; a sequence profile writes frames
    # into the output folder. Previews are always single PNGs (no mux).
    out_fmt = str(render.get("output_format", "PNG")).strip().upper()
    is_movie = preview_frame <= 0 and out_fmt in ("MPEG4", "QUICKTIME")
    out_path = cfg["output_path"]
    if is_movie:
        frame_dir = tempfile.mkdtemp(prefix="c4d_movie_")
    else:
        frame_dir = out_path
        os.makedirs(frame_dir, exist_ok=True)

    log(f"Rendering {len(frames)} frame(s) at {xr}x{yr} with Redshift"
        + (f" -> {out_fmt} movie" if is_movie else " (image sequence)"))
    for idx, f in enumerate(frames, start=1):
        # Swap in this frame's still for every mapped material.
        for (graph, port, vid, tag) in mapped:
            still = os.path.join(tmp_dir, f"clip{tag}_{f:04d}.png")
            if _extract_frame(ffmpeg, vid, f, fps, still):
                _set_still(graph, port, still)
        doc.SetTime(c4d.BaseTime(f, fps))
        doc.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_NONE)
        bmp = c4d.bitmaps.BaseBitmap()
        bmp.Init(xr, yr, 24)
        res = documents.RenderDocument(doc, rd.GetDataInstance(), bmp, c4d.RENDERFLAGS_EXTERNAL)
        frame_path = os.path.join(frame_dir, f"{idx:06d}.png" if is_movie else f"{f:04d}.png")
        bmp.Save(frame_path, c4d.FILTER_PNG, c4d.BaseContainer(), c4d.SAVEBIT_0)
        log(f"Frame {f} -> {frame_path} (ok={res == c4d.RENDERRESULT_OK})")

    if is_movie:
        log(f"Encoding movie -> {out_path}")
        if _mux_movie(ffmpeg, frame_dir, fps, out_path, out_fmt,
                      str(render.get("video_codec", "")) or str(render.get("codec", "H264")),
                      str(render.get("video_quality", "HIGH"))):
            log(f"Movie written: {out_path}")
        shutil.rmtree(frame_dir, ignore_errors=True)
    log("Render finished successfully")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR: " + traceback.format_exc())
        sys.exit(1)
