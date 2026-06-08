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
                port.SetDefaultValue(maxon.Float64(val))
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
        path_port.SetDefaultValue(maxon.Url(image_path))
        transaction.Commit()
    except Exception:
        transaction.Commit()


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
    width = int(render.get("width", 1920))
    height = int(render.get("height", 1080))
    pct = max(1, min(100, int(render.get("resolution_percentage", 100))))
    xr = max(1, width * pct // 100)
    yr = max(1, height * pct // 100)
    rd[c4d.RDATA_XRES] = float(xr)
    rd[c4d.RDATA_YRES] = float(yr)

    out_dir = cfg["output_path"]
    os.makedirs(out_dir, exist_ok=True)
    fstart = int(render.get("frame_start", 1))
    fend = int(render.get("frame_end", 1))
    step = max(1, int(render.get("frame_step", 1)))
    preview_frame = int(cfg.get("preview_frame", 0) or 0)
    frames = [preview_frame] if preview_frame > 0 else list(range(fstart, fend + 1, step))

    log(f"Rendering {len(frames)} frame(s) at {xr}x{yr} with Redshift")
    for f in frames:
        # Swap in this frame's still for every mapped material.
        for (graph, port, vid, tag) in mapped:
            still = os.path.join(tmp_dir, f"clip{tag}_{f:04d}.png")
            if _extract_frame(ffmpeg, vid, f, fps, still):
                _set_still(graph, port, still)
        doc.SetTime(c4d.BaseTime(f, fps))
        doc.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_NONE)
        bmp = c4d.bitmaps.BaseBitmap()
        bmp.Init(xr, yr, 24)
        res = documents.RenderDocument(doc, rd.GetData(), bmp, c4d.RENDERFLAGS_EXTERNAL)
        out_path = os.path.join(out_dir, f"{f:04d}.png")
        bmp.Save(out_path, c4d.FILTER_PNG, c4d.BaseContainer(), c4d.SAVEBIT_0)
        log(f"Frame {f} -> {out_path} (ok={res == c4d.RENDERRESULT_OK})")
    log("Render finished successfully")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR: " + traceback.format_exc())
        sys.exit(1)
