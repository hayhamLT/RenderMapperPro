"""Media probing + ffmpeg tool discovery (UI-free).

Locates the bundled/installed ffmpeg+ffprobe and answers questions about
clips: does it carry audio, how many frames, what fps. Everything is cached.
Also holds the small bits of platform glue the panels need (file-manager
naming/reveal, the modifier-key glyph).
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import struct
import subprocess
import sys
from pathlib import Path

# rec.709 colour tags + faststart for movie outputs, so QuickTimes look identical
# across players/NLEs. (c4d_worker.py mirrors this — it runs in c4dpy and can't
# import this module.)
REC709_FASTSTART_ARGS = [
    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
    "-movflags", "+faststart",
]


# Modifier-key glyph for shortcut hints in menu/button text: Cmd on macOS,
# "Ctrl+" elsewhere. Qt itself maps QKeySequence("Ctrl+D") to the right key
# per platform; this is only for the human-readable label.
def _ffmpeg_platform_dir() -> str:
    """Vendor sub-directory name for the running platform, e.g. 'darwin-arm64'."""
    sysn = "linux" if sys.platform.startswith("linux") else sys.platform  # darwin|win32|linux
    mach = platform.machine().lower()
    arch = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(mach, mach)
    return f"{sysn}-{arch}"


# Modifier-key glyph for shortcut hints in menu/button text: ⌘ on macOS,
# "Ctrl+" elsewhere. Qt itself maps QKeySequence("Ctrl+D") to the right key
# per platform; this is only for the human-readable label.
MOD_LABEL = "⌘" if sys.platform == "darwin" else "Ctrl+"


# Name of the OS file manager, used in menu/button labels so they read
# natively on each platform ("Reveal in Finder" vs "Show in Explorer").
def file_manager_name() -> str:
    if sys.platform == "darwin":
        return "Finder"
    if os.name == "nt":
        return "Explorer"
    return "File Manager"


def reveal_in_file_manager(path) -> None:
    """Open the OS file manager with ``path`` selected (cross-platform)."""
    p = Path(path)
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(p)]
    elif os.name == "nt":
        cmd = ["explorer", "/select,", str(p)]
    else:
        cmd = ["xdg-open", str(p.parent)]
    # These launchers hand off to the file manager and exit immediately; run()
    # reaps them cleanly (no ResourceWarning) while the timeout guards a hang.
    try:
        subprocess.run(cmd, check=False, timeout=10)
    except Exception:
        pass


# Common system install locations to check after PATH (GUI apps launched from
# Finder often have a minimal PATH that misses Homebrew / MacPorts).
_FFMPEG_SYS_DIRS = [
    "/opt/homebrew/bin",   # Apple-silicon Homebrew
    "/usr/local/bin",      # Intel Homebrew
    "/usr/bin",
    "/opt/local/bin",      # MacPorts
    r"C:\ffmpeg\bin",
]

_ffmpeg_tool_cache: dict[str, str | None] = {}


def find_ffmpeg_tool(name: str) -> str | None:
    """Locate an ffmpeg-family binary ('ffmpeg' or 'ffprobe').

    Resolution order, so the copy that ships with the app always wins:
      1. bundled binary (PyInstaller _MEIPASS, when frozen)
      2. a ``vendor/ffmpeg`` dir next to the source (dev runs)
      3. PATH
      4. common system install locations
    Returns an absolute path, or None if nothing is found.
    """
    if name in _ffmpeg_tool_cache:
        return _ffmpeg_tool_cache[name]
    exe = name + (".exe" if sys.platform.startswith("win") else "")
    candidates: list[Path] = []

    if getattr(sys, "frozen", False):
        mei = Path(getattr(sys, "_MEIPASS", ""))
        candidates += [mei / exe, mei / "ffmpeg" / exe, mei / "vendor" / "ffmpeg" / exe]

    here = Path(__file__).parent
    vroot = here / "vendor" / "ffmpeg"
    candidates += [vroot / _ffmpeg_platform_dir() / exe, vroot / exe]

    resolved: str | None = None
    for c in candidates:
        if c.is_file():
            try:
                os.chmod(c, 0o755)  # ensure the bundled binary is executable
            except Exception:
                pass
            resolved = str(c)
            break

    if resolved is None:
        resolved = shutil.which(name)
    if resolved is None:
        for d in _FFMPEG_SYS_DIRS:
            sys_candidate = os.path.join(d, exe)
            if os.path.exists(sys_candidate):
                resolved = sys_candidate
                break

    _ffmpeg_tool_cache[name] = resolved
    return resolved


def _find_ffprobe() -> str | None:
    return find_ffmpeg_tool("ffprobe")


def evenly_spaced(items: list, n: int) -> list:
    """Pick up to ``n`` items spread evenly across ``items`` (always the first)."""
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[min(int(i * step), len(items) - 1)] for i in range(n)]


def _probe_frame_count(src: str) -> int:
    """Best-effort frame count of a video via ffprobe (container metadata first,
    then a full count). 0 if unavailable."""
    ffprobe = find_ffmpeg_tool("ffprobe")
    if not ffprobe:
        return 0
    from core.utils import subprocess_creation_flags
    for args in (
        ["-show_entries", "stream=nb_frames"],
        ["-count_frames", "-show_entries", "stream=nb_read_frames"],
    ):
        try:
            r = subprocess.run(
                [ffprobe, "-v", "error", "-select_streams", "v:0", *args,
                 "-of", "default=nokey=1:noprint_wrappers=1", src],
                capture_output=True, text=True, timeout=120,
                creationflags=subprocess_creation_flags())
            val = (r.stdout or "").strip().splitlines()
            if val and val[0].isdigit() and int(val[0]) > 0:
                return int(val[0])
        except Exception:
            continue
    return 0


def build_contact_sheet(src: str, dest: str, cols: int = 4, rows: int = 3) -> bool:
    """Render a tiled contact-sheet PNG (``cols``×``rows``) sampling frames evenly
    across a movie file or an image-sequence folder. Returns True on success."""
    ffmpeg = find_ffmpeg_tool("ffmpeg")
    if not ffmpeg:
        return False
    from core.utils import subprocess_creation_flags
    cells = max(1, cols * rows)
    p = Path(src)
    try:
        if p.is_dir():
            frames = sorted(p.glob("*.png")) or sorted(p.glob("*.jpg")) \
                or sorted(p.glob("*.jpeg"))
            picks = evenly_spaced(frames, cells)
            if not picks:
                return False
            import tempfile
            listing = "\n".join(f"file '{f.as_posix()}'" for f in picks)
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as tf:
                tf.write(listing)
                list_path = tf.name
            try:
                r = subprocess.run(
                    [ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                     "-vf", f"scale=320:-1,tile={cols}x{rows}", "-frames:v", "1", dest],
                    capture_output=True, text=True, timeout=180,
                    creationflags=subprocess_creation_flags())
            finally:
                Path(list_path).unlink(missing_ok=True)
            return r.returncode == 0 and Path(dest).exists()
        if not p.is_file():
            return False
        total = _probe_frame_count(src)
        step = max(1, total // cells) if total else 24
        r = subprocess.run(
            [ffmpeg, "-y", "-i", src,
             "-frames:v", "1", "-fps_mode", "vfr",
             "-vf", f"select=not(mod(n\\,{step})),scale=320:-1,tile={cols}x{rows}",
             dest],
            capture_output=True, text=True, timeout=240,
            creationflags=subprocess_creation_flags())
        return r.returncode == 0 and Path(dest).exists()
    except Exception:
        return False


def _mp4_has_audio(path: str) -> bool:
    """ffprobe-free audio detection for MP4/MOV: locate the ``moov`` atom and
    look for a sound-track handler (``soun``). Reads only the metadata atom."""
    try:
        with open(path, "rb") as f:
            total = os.fstat(f.fileno()).st_size
            pos = 0
            while pos < total:
                f.seek(pos)
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                size, = struct.unpack(">I", hdr[:4])
                btype = hdr[4:8]
                header = 8
                if size == 1:
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size, = struct.unpack(">Q", ext)
                    header = 16
                elif size == 0:
                    size = total - pos
                if size < header:
                    break
                if btype == b"moov":
                    body = f.read(min(size - header, 64 * 1024 * 1024))
                    return b"soun" in body
                pos += size
    except Exception:
        pass
    return False


_audio_probe_cache: dict[str, bool] = {}
_MP4_AUDIO_EXTS = {".mp4", ".mov", ".m4v", ".m4a", ".qt"}


def video_has_audio(path: str) -> bool:
    """True if ``path`` contains at least one audio stream (cached).

    Prefers ffprobe (handles every container); falls back to a built-in
    MP4/MOV atom scan when ffprobe isn't available.
    """
    if not path:
        return False
    cached = _audio_probe_cache.get(path)
    if cached is not None:
        return cached
    result = False
    if os.path.exists(path):
        ffprobe = _find_ffprobe()
        if ffprobe:
            try:
                out = subprocess.check_output(
                    [
                        ffprobe, "-v", "quiet", "-print_format", "json",
                        "-show_streams", "-select_streams", "a", path,
                    ],
                    text=True,
                    timeout=10,
                )
                result = bool(json.loads(out).get("streams"))
            except Exception:
                result = False
        elif Path(path).suffix.lower() in _MP4_AUDIO_EXTS:
            result = _mp4_has_audio(path)
    _audio_probe_cache[path] = result
    return result


def _parse_mp4_info(path: str) -> tuple[int, float] | None:
    """Return (total_frames, fps) for a video file.
    Tries ffprobe first (handles all containers), falls back to hand-rolled MP4 parser.
    """
    # ── ffprobe fast path ────────────────────────────────────────────────────
    ffprobe = find_ffmpeg_tool("ffprobe")
    if ffprobe:
        try:
            out = subprocess.check_output(
                [
                    ffprobe, "-v", "quiet", "-print_format", "json",
                    "-show_streams", "-select_streams", "v:0", path,
                ],
                text=True,
                timeout=10,
            )
            data = json.loads(out)
            streams = data.get("streams", [])
            if streams:
                s = streams[0]
                # fps from avg_frame_rate (e.g. "30000/1001")
                fps_str = s.get("avg_frame_rate", "") or s.get("r_frame_rate", "")
                fps: float = 0.0
                if "/" in fps_str:
                    num, den = fps_str.split("/", 1)
                    if int(den) > 0:
                        fps = round(int(num) / int(den), 3)
                nb_frames = s.get("nb_frames", "")
                if nb_frames and nb_frames.isdigit() and int(nb_frames) > 0:
                    return int(nb_frames), fps
                # Fallback: duration × fps
                dur = float(s.get("duration", 0) or 0)
                if dur > 0 and fps > 0:
                    return int(round(dur * fps)), fps
        except Exception:
            pass  # Fall through to hand-rolled parser

    # ── Hand-rolled MP4/MOV atom parser ─────────────────────────────────────
    results: dict = {}

    def walk(f, end: int, depth: int = 0) -> None:
        while f.tell() < end:
            pos = f.tell()
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            size, = struct.unpack(">I", hdr[:4])
            btype = hdr[4:8].decode("latin-1")
            if size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    break
                size, = struct.unpack(">Q", ext)
                hlen = 16
            elif size == 0:
                size = end - pos
                hlen = 8
            else:
                hlen = 8
            ce = pos + size
            if size < hlen:
                break
            if btype in {"moov", "trak", "mdia", "minf", "stbl"} and depth < 8:
                walk(f, ce, depth + 1)
            elif btype == "mdhd":
                v = struct.unpack("B", f.read(1))[0]
                f.read(3)
                if v == 1:
                    f.read(16)
                    ts, = struct.unpack(">I", f.read(4))
                else:
                    f.read(8)
                    ts, = struct.unpack(">I", f.read(4))
                if ts > 0:
                    results["ts"] = ts
            elif btype == "stts":
                f.read(4)
                cnt, = struct.unpack(">I", f.read(4))
                entries = [struct.unpack(">II", f.read(8)) for _ in range(min(cnt, 200_000))]
                if "stts" not in results:
                    results["stts"] = entries
            f.seek(ce)

    try:
        with open(path, "rb") as f:
            walk(f, os.path.getsize(path))
        if "stts" in results and "ts" in results:
            stts = results["stts"]
            ts = results["ts"]
            total = sum(sc for sc, _ in stts)
            if total > 0 and ts > 0:
                avg_dur = sum(sc * sd for sc, sd in stts) / total
                fps = round(ts / avg_dur, 3) if avg_dur > 0 else 0.0
                return total, fps
    except Exception:
        return None
    return None


# Common real-world frame rates we snap detected values to.
_STANDARD_FPS = [23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94, 60.0, 23.98, 24000 / 1001]


def _normalize_fps(raw: float | None, default: int = 30) -> int:
    """Return a trustworthy integer fps for the UI: snap a detected value to the
    nearest standard rate when it's close, otherwise fall back to ``default``.

    The hand-rolled MP4 parser can produce odd values, so we only accept a
    reading that lands near a real frame rate."""
    if not raw or raw <= 0 or raw > 1000:
        return default
    nearest = min(_STANDARD_FPS, key=lambda s: abs(s - raw))
    if abs(nearest - raw) <= max(0.6, nearest * 0.02):
        return int(round(nearest))
    # Plausible but non-standard integer rate (e.g. 12, 15) — keep it.
    if 1 <= raw <= 120 and abs(raw - round(raw)) <= 0.05:
        return int(round(raw))
    return default



_video_size_cache: dict[str, tuple[int, int] | None] = {}


def probe_video_size(path: str) -> tuple[int, int] | None:
    """(width, height) of a clip's video stream via ffprobe, cached. None if
    unknown (no ffprobe, unreadable file, audio-only…)."""
    if not path:
        return None
    if path in _video_size_cache:
        return _video_size_cache[path]
    result: tuple[int, int] | None = None
    ffprobe = _find_ffprobe()
    if ffprobe and os.path.exists(path):
        try:
            out = subprocess.check_output(
                [ffprobe, "-v", "quiet", "-print_format", "json",
                 "-show_streams", "-select_streams", "v:0", path],
                text=True, timeout=10)
            streams = json.loads(out).get("streams") or []
            if streams:
                w = int(streams[0].get("width") or 0)
                h = int(streams[0].get("height") or 0)
                if w > 0 and h > 0:
                    result = (w, h)
        except Exception:
            result = None
    _video_size_cache[path] = result
    return result
