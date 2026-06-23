"""Cinema 4D scene discovery — run under c4dpy.

Loads a .c4d and prints the same ``DISCOVERY_JSON:`` payload the Blender
discovery script emits (materials, cameras, settings), so the app's existing
parser handles both backends.

    printf '1\\n' | c4dpy c4d_discover.py /path/to/scene.c4d
"""
import json
import sys

import c4d
from c4d import documents

PREFIX = "DISCOVERY_JSON:"


def log(message: str) -> None:
    print(f"[c4d-discover] {message}")


# Standard C4D camera (Ocamera) plus the Redshift camera object type.
_CAMERA_TYPES = {c4d.Ocamera, 1057516}


def _is_camera(obj) -> bool:
    if obj.GetType() in _CAMERA_TYPES:
        return True
    try:
        return obj.CheckType(c4d.Ocamera)
    except Exception:
        return False


def _collect_cameras(doc) -> list:
    cameras: list = []

    def walk(obj):
        while obj:
            if _is_camera(obj) and obj.GetName():
                cameras.append(obj.GetName())
            child = obj.GetDown()
            if child:
                walk(child)
            obj = obj.GetNext()

    walk(doc.GetFirstObject())
    return cameras


def _settings(doc) -> dict:
    s: dict = {}
    try:
        fps = int(doc.GetFps())
        rd = doc.GetActiveRenderData()
        s["fps"] = fps
        s["frame_start"] = int(rd[c4d.RDATA_FRAMEFROM].GetFrame(fps))
        s["frame_end"] = int(rd[c4d.RDATA_FRAMETO].GetFrame(fps))
        s["frame_step"] = max(1, int(rd[c4d.RDATA_FRAMESTEP]))
        s["width"] = int(rd[c4d.RDATA_XRES])
        s["height"] = int(rd[c4d.RDATA_YRES])
        # Which C4D render engine the scene uses.
        engines = {0: "Standard", 1023342: "Physical", 1036219: "Redshift"}
        s["renderer"] = engines.get(int(rd[c4d.RDATA_RENDERENGINE]), "Redshift")
        # Adopt the scene's Redshift sampling/optimization into the UI.
        vp = rd.GetFirstVideoPost()
        while vp:
            if vp.GetType() == 1036219:
                s["samples"] = int(vp[c4d.REDSHIFT_RENDERER_UNIFIED_MAX_SAMPLES])
                s["rs_min_samples"] = int(vp[c4d.REDSHIFT_RENDERER_UNIFIED_MIN_SAMPLES])
                s["rs_threshold"] = float(vp[c4d.REDSHIFT_RENDERER_UNIFIED_ADAPTIVE_ERROR_THRESHOLD])
                s["use_denoise"] = bool(vp[c4d.REDSHIFT_RENDERER_DENOISE_ENABLED])
                s["rs_gi_enabled"] = bool(vp[c4d.REDSHIFT_RENDERER_GI_ENABLED])
                s["rs_gi_bounces"] = int(vp[c4d.REDSHIFT_RENDERER_NUM_GI_BOUNCES])
                s["rs_ray_depth"] = int(vp[c4d.REDSHIFT_RENDERER_MAX_TRACE_DEPTH_COMBINED])
                break
            vp = vp.GetNext()
    except Exception as exc:  # never let settings probing break discovery
        log(f"settings probe warning: {exc}")
    # (Material-type is no longer probed: standard C4D materials are auto-converted
    # to a full-bright Redshift emission at render time, so any mapped material shows
    # the clip — there's nothing to warn about up front.)
    return s


def main() -> None:
    if len(sys.argv) < 2:
        raise RuntimeError("Missing scene path argument")
    path = sys.argv[1]
    log(f"Loading scene: {path}")
    doc = documents.LoadDocument(path, c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS)
    if doc is None:
        raise RuntimeError(f"Could not load scene: {path}")
    materials = sorted({m.GetName() for m in doc.GetMaterials() if m and m.GetName()})
    cameras = sorted(set(_collect_cameras(doc)))
    payload = {"materials": materials, "cameras": cameras, "settings": _settings(doc)}
    print(PREFIX + json.dumps(payload))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"ERROR: {exc}")
        sys.exit(1)
