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



# ── Auto-match media files to materials by name ─────────────────────────────
# Primary rule (per user's workflow): a file matches a material when the
# material's name appears in the file's name. We normalise both, score the
# candidates, and assign greedily one-to-one so a confident guess wins and
# ambiguous ones are left for manual mapping.

_MAT_AFFIX_TOKENS = {"m", "mat", "material", "mtl", "shader"}


def normalize_match_name(name: str) -> str:
    """Lower-case, drop a Blender duplicate suffix (.001), split on any
    non-alphanumeric run, and strip leading/trailing material-ish tokens."""
    name = re.sub(r"\.\d{2,}$", "", str(name)).lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", name) if t]
    while tokens and tokens[0] in _MAT_AFFIX_TOKENS:
        tokens = tokens[1:]
    while tokens and tokens[-1] in _MAT_AFFIX_TOKENS:
        tokens = tokens[:-1]
    return " ".join(tokens)


def match_score(material: str, file_stem: str) -> float:
    """Score how strongly a file (by stem) belongs to a material (0..1).
    Highest for an exact normalized match, then whole-word containment, then
    a looser substring. Returns 0 for no match / too-short material names."""
    nm = normalize_match_name(material)
    ns = normalize_match_name(file_stem)
    if not nm or not ns or len(nm) < 2:
        return 0.0
    if nm == ns:
        return 1.0
    coverage = len(nm) / max(len(ns), 1)        # how much of the filename the material covers
    mat_tokens, stem_tokens = nm.split(), set(ns.split())
    if all(t in stem_tokens for t in mat_tokens):   # every material word is a whole word in the file
        return 0.60 + 0.40 * coverage
    if nm in ns:                                     # appears as a substring anywhere
        return 0.40 + 0.30 * coverage
    return 0.0


def auto_match_media_to_materials(
    materials: list[str],
    files: list[str],
    min_score: float = 0.45,
) -> dict[str, str]:
    """Return {material_name: file_path} for confident name matches. Each file
    is used at most once and each material matched at most once (greedy by
    descending score); anything below ``min_score`` is left unmatched."""
    candidates = []
    for mat in materials:
        for f in files:
            stem = Path(f).stem
            score = match_score(mat, stem)
            if score >= min_score:
                # Tie-break: prefer the shorter filename (tighter fit).
                candidates.append((score, -len(stem), mat, f))
    candidates.sort(reverse=True)
    result: dict[str, str] = {}
    used_files: set[str] = set()
    for _score, _neg_len, mat, f in candidates:
        if mat in result or f in used_files:
            continue
        result[mat] = f
        used_files.add(f)
    return result


# ── Watch-folder version handling ───────────────────────────────────────────
# Files that share a base name but differ by a version token (Screen_v1.mp4,
# Screen_v2.mp4, Screen_3.mp4) are treated as versions of the same clip. The
# highest version wins (ties broken by newest modification time), so a watch
# folder always uses the latest and supersedes an older one already in use.

_VERSION_V_RE = re.compile(r"[._\-\s]*v(\d+)", re.I)   # _v2, v002, -V3
_VERSION_TRAIL_RE = re.compile(r"[._\-\s]+(\d+)$")      # _2, " 3", -10


def parse_version(stem: str) -> tuple[str, int]:
    """Split a filename stem into (normalized base, version number).
    No version token → version 0."""
    s = str(stem)
    matches = list(_VERSION_V_RE.finditer(s))
    if matches:
        last = matches[-1]
        version = int(last.group(1))
        base = s[:last.start()] + s[last.end():]
    else:
        trail = _VERSION_TRAIL_RE.search(s)
        if trail:
            version = int(trail.group(1))
            base = s[:trail.start()]
        else:
            version, base = 0, s
    base = re.sub(r"[^a-z0-9]+", " ", base.lower()).strip()
    return base, version


def _version_rank(path: str, mtimes: dict | None) -> tuple[int, float]:
    _base, version = parse_version(Path(path).stem)
    return version, float((mtimes or {}).get(path, 0.0))


def latest_by_base(files: list[str], mtimes: dict | None = None) -> dict[str, str]:
    """Pick the latest file for each base name (highest version, then newest)."""
    best: dict[str, tuple[tuple[int, float], str]] = {}
    for f in files:
        base, _v = parse_version(Path(f).stem)
        rank = _version_rank(f, mtimes)
        if base not in best or rank > best[base][0]:
            best[base] = (rank, f)
    return {base: val[1] for base, val in best.items()}


def reconcile_versions(
    current_videos: list[str],
    folder_files: list[str],
    mtimes: dict | None = None,
) -> tuple[list[str], dict[str, str], list[str]]:
    """Given the clips already loaded and the files now in the watch folder,
    return (videos_after, replacements, added):
      - replacements: {old_path: new_path} where a newer version supersedes a
        clip already in use (same base, higher rank).
      - added: brand-new clips (base not already loaded) at their latest version.
    Older versions are dropped from videos_after."""
    latest = latest_by_base(folder_files, mtimes)
    cur_by_base: dict[str, str] = {}
    for v in current_videos:
        base, _v = parse_version(Path(v).stem)
        # if two loaded clips share a base, keep the higher-ranked one as current
        if base not in cur_by_base or _version_rank(v, mtimes) > _version_rank(cur_by_base[base], mtimes):
            cur_by_base[base] = v

    replacements: dict[str, str] = {}
    added: list[str] = []
    for base, new_file in latest.items():
        old = cur_by_base.get(base)
        if old is None:
            if new_file not in current_videos:
                added.append(new_file)
        elif old != new_file and _version_rank(new_file, mtimes) > _version_rank(old, mtimes):
            replacements[old] = new_file

    videos_after = []
    for v in current_videos:
        videos_after.append(replacements.get(v, v))
    for f in added:
        if f not in videos_after:
            videos_after.append(f)
    # de-dup preserving order
    seen: set[str] = set()
    videos_after = [v for v in videos_after if not (v in seen or seen.add(v))]
    return videos_after, replacements, added
