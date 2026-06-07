from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

# Maps Blender output_format value → file extension (empty = image sequence folder)
OUTPUT_FORMAT_EXT: dict[str, str] = {
    "MPEG4": ".mp4",
    "QUICKTIME": ".mov",
    "PNG": "",
    "OPEN_EXR": "",
    "OPEN_EXR_MULTILAYER": "",
    "BMP": ".bmp",
    "JPEG": ".jpg",
    "JPEG2000": ".jp2",
    "TIFF": ".tif",
    "CINEON": ".cin",
    "DPX": ".dpx",
    "HDR": ".hdr",
    "WEBP": ".webp",
    "AVI_JPEG": ".avi",
    "AVI_RAW": ".avi",
    "FFMPEG": ".mp4",
}

# Supported token names shown in placeholder
OUTPUT_TOKENS = "{video}  {scene}  {camera}  {date}"


def slugify_filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return name or "output"


def expand_output_tokens(
    template: str,
    scene_stem: str,
    video_stem: str,
    extra: dict | None = None,
) -> str:
    today = date.today().strftime("%Y-%m-%d")
    out = (
        template
        .replace("{scene}", scene_stem)
        .replace("{video}", video_stem)
        .replace("{name}", video_stem)
        .replace("{date}", today)
    )
    for key, value in (extra or {}).items():
        out = out.replace("{" + key + "}", str(value))
    return out


def ext_for_format(output_format: str) -> str:
    """Return the file extension (with dot) for a given Blender output_format string."""
    return OUTPUT_FORMAT_EXT.get(output_format.upper(), ".mp4")


def resolve_output_path(
    output_input: str,
    scene_path: str,
    video_path: str,
    is_batch: bool,
    job_label: str = "",
    output_format: str = "MPEG4",
    extra_tokens: dict | None = None,
) -> str:
    scene_stem = slugify_filename(Path(scene_path).stem)
    video_stem = slugify_filename(Path(video_path).stem)
    label_stem = slugify_filename(job_label) if job_label.strip() else video_stem

    ext = ext_for_format(output_format)
    is_sequence = ext == ""

    # Make extra token values filename-safe before substitution.
    safe_extra = {
        k: (slugify_filename(str(v)) if isinstance(v, str) else str(v))
        for k, v in (extra_tokens or {}).items()
    }

    # Expand tokens before resolving path
    raw = expand_output_tokens(
        output_input.strip(),
        scene_stem=scene_stem,
        video_stem=video_stem,
        extra=safe_extra,
    )

    # A bare/relative template (e.g. tokens only) is placed next to the source
    # video rather than the process working directory.
    output = Path(raw).expanduser()
    if raw and not output.is_absolute():
        base_dir = Path(video_path).expanduser().parent
        output = base_dir / output
    output = output.resolve()
    auto_name = f"{scene_stem}__{label_stem}"

    if is_sequence:
        # Image sequences always go into a folder
        folder = output if not output.suffix else output.parent / output.stem
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder)

    if is_batch:
        output.mkdir(parents=True, exist_ok=True)
        return str(output / f"{auto_name}{ext}")

    # Input already has the right extension → keep it
    if output.suffix.lower() == ext:
        output.parent.mkdir(parents=True, exist_ok=True)
        return str(output)

    # Input has a different extension → correct it
    if output.suffix:
        corrected = output.with_suffix(ext)
        corrected.parent.mkdir(parents=True, exist_ok=True)
        return str(corrected)

    # No extension: an existing directory gets an auto-named file inside it;
    # otherwise the (token-built) name itself is used as the output filename.
    if output.is_dir():
        return str(output / f"{auto_name}{ext}")
    output.parent.mkdir(parents=True, exist_ok=True)
    return str(output) + ext


def file_exists(path: str) -> bool:
    return os.path.isfile(os.path.expanduser(path))


def find_deadlinecommand() -> str | None:
    import shutil
    import sys

    # 1. Check if it's already on PATH
    cmd = shutil.which("deadlinecommand")
    if cmd:
        return cmd

    # 2. Check standard search locations depending on OS
    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Thinkbox\Deadline10\bin\deadlinecommand.exe",
            r"C:\Program Files\Thinkbox\Deadline\bin\deadlinecommand.exe",
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Thinkbox/Deadline10/bin/deadlinecommand",
            "/Applications/Thinkbox/Deadline10/Resources/deadlinecommand",
            "/Applications/Thinkbox/Deadline/bin/deadlinecommand",
            "/Applications/Thinkbox/Deadline/Resources/deadlinecommand",
        ]
    else:
        candidates = [
            "/opt/Thinkbox/Deadline10/bin/deadlinecommand",
            "/opt/Thinkbox/Deadline/bin/deadlinecommand",
        ]

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    # 3. Fallback: Search in the standard parent directory (e.g. /Applications/Thinkbox)
    try:
        if sys.platform == "darwin":
            parent = Path("/Applications/Thinkbox")
            if parent.exists():
                for p in parent.rglob("deadlinecommand"):
                    if p.is_file() and os.access(p, os.X_OK):
                        return str(p)
        elif sys.platform == "win32":
            parent = Path(r"C:\Program Files\Thinkbox")
            if parent.exists():
                for p in parent.rglob("deadlinecommand.exe"):
                    if p.is_file():
                        return str(p)
        else:
            parent = Path("/opt/Thinkbox")
            if parent.exists():
                for p in parent.rglob("deadlinecommand"):
                    if p.is_file() and os.access(p, os.X_OK):
                        return str(p)
    except Exception:
        pass

    return None

