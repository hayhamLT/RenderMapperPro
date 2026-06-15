"""User-facing reporting helpers — pure, UI-free, unit-testable.

Maps renderer errors to plain-language "what to try" hints and formats durations
for captions. Extracted from the Qt window so the logic can be tested without
instantiating a widget.
"""
from __future__ import annotations

# (substring needles, hint). First matching rule wins.
_ERROR_HINT_RULES: list[tuple[tuple[str, ...], str]] = [
    (("out of memory", "cuda error: out of memory", "memoryerror", "vram",
      "cuda_error_out_of_memory", "out of gpu memory"),
     "The GPU or system ran out of memory. Lower the resolution or sample "
     "count, or set Device to CPU in Render settings."),
    (("no space left", "errno 28", "disk full"),
     "The output disk is full. Free up space or pick a different output folder."),
    (("permission denied", "errno 13", "access is denied"),
     "Permission denied writing the output. Choose a different output folder "
     "or check its permissions."),
    (("no such file", "filenotfounderror", "cannot read", "unable to open",
      "could not open", "errno 2"),
     "A scene or media file couldn't be found — it may have moved or still be "
     "syncing. Re-locate it, then try again."),
    (("unknown encoder", "codec", "ffmpeg", "unsupported pixel format",
      "no video stream"),
     "The video codec/format isn't available. Try a different codec or output "
     "format (e.g. H.264 MP4)."),
    (("created in a newer", "blend file format", "version mismatch",
      "unsupported .blend", "blender version"),
     "The scene may be from a newer Blender/Cinema 4D version than the one "
     "configured. Open it once in the matching app, or point Properties at a "
     "newer build."),
    (("material not found", "no material named", "cannot find material",
      "unknown material"),
     "A material referenced by the mapping wasn't found in the scene. Re-scan "
     "the scene and check the mappings."),
    (("license", "maxon app"),
     "Cinema 4D licensing failed — make sure you're signed in to the Maxon app "
     "on this machine."),
]


def friendly_error_hint(text: str) -> str:
    """Map a common renderer failure to a plain-language 'what to try' line.
    Returns "" when nothing matches (the raw error is always shown anyway)."""
    t = (text or "").lower()
    for needles, hint in _ERROR_HINT_RULES:
        if any(n in t for n in needles):
            return hint
    return ""


def format_duration(seconds: float) -> str:
    """Compact human duration: '45s', '3m05s', '1h02m'."""
    seconds = int(max(0, seconds))
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"
