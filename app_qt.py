from __future__ import annotations

import dataclasses
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    Qt,
    QThread,
    QTimer,
    Signal,
    QEvent,
    QUrl,
    QByteArray,
    QSize,
    QPoint,
    QRect,
    QPropertyAnimation,
    QEasingCurve,
)
from PySide6.QtGui import QAction, QActionGroup, QFont, QIcon, QPixmap, QColor, QPainter, QPen, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QToolBar,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

import theme as T
import icons

try:
    from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtWidgets import QStackedWidget
    _HAS_MULTIMEDIA = True
except Exception:
    _HAS_MULTIMEDIA = False

from core.discovery import discover_scene_elements
from core.models import (
    JobConfig,
    MaterialVideoAssignment,
    RenderOptions,
    VIDEO_MAPPING_MODE_EMISSION,
)
from core.runner import run_blender_job, submit_deadline_job
from core.utils import file_exists, resolve_output_path, ext_for_format, OUTPUT_TOKENS, find_deadlinecommand, auto_match_media_to_materials, reconcile_versions
from core.utils import version_tuple as _version_tuple, update_platform_key as _update_platform_key


OUTPUT_PROFILES: dict[str, tuple[str, str]] = {
    "H264 MP4": ("MPEG4", "H264"),
    "ProRes MOV": ("QUICKTIME", "PRORES"),
    "PNG Sequence": ("PNG", "NONE"),
    "OpenEXR Sequence": ("OPEN_EXR", "NONE"),
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
IMAGE_MEDIA_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".bmp", ".webp", ".tga", ".hdr"}
SCENE_EXTENSIONS = {".blend", ".c4d", ".fbx", ".obj", ".glb", ".gltf", ".usd", ".usda", ".usdc", ".abc", ".stl", ".ply"}
LINK_COLORS = [
    "#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#1abc9c",
    "#3498db", "#9b59b6", "#e91e63", "#00bcd4", "#8bc34a",
]
FRAME_RE = [re.compile(r"Fra:(\d+)"), re.compile(r"Frame\s+(\d+)", re.I)]
DISCOVERY_TIMEOUT = 600
PROFILE_PATH = Path.home() / ".blender_video_mapper" / "profile.json"
PRESETS_DIR = Path.home() / ".blender_video_mapper" / "presets"
HISTORY_PATH = Path.home() / ".blender_video_mapper" / "history.json"
# Branded file extensions (JSON underneath) for user-facing Save/Open.
PROJECT_EXT = ".rmproj"      # full project: scene, clips, mappings, queue
PRESET_EXT = ".rmpreset"     # reusable render-settings recipe
REPORTS_DIR = Path.home() / ".blender_video_mapper" / "reports"
LOG_PATH = Path.home() / ".blender_video_mapper" / "logs" / "app_qt.log"
APP_NAME = "Render Mapper Pro"
APP_VERSION = "1.4.9"
RUNTIME_ROOT = Path.home() / ".blender_video_mapper" / "runtime"
BLENDER_RUNTIME_VERSION = "5.1.0"
PROFILE_VERSION = 3
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

# Active palette shared across the module so panels can tint their icons at
# build time and re-tint when the user switches theme/accent. The main window
# overwrites this in _apply_theme before any panel is constructed.
_ACTIVE_PALETTE: T.Palette = T.build_palette("dark", T.ACCENT_ORANGE)


def active_palette() -> T.Palette:
    return _ACTIVE_PALETTE


def set_active_palette(pal: T.Palette) -> None:
    global _ACTIVE_PALETTE
    _ACTIVE_PALETTE = pal


def _make_app_icon() -> QIcon:
    return icons.app_icon()


def _norm_blender(candidate: str) -> Optional[str]:
    candidate = candidate.strip()
    if not candidate:
        return None
    exp = Path(os.path.expanduser(candidate))
    if exp.suffix.lower() == ".app":
        for p in (exp / "Contents/MacOS/Blender", exp / "Contents/MacOS/blender"):
            if p.exists() and p.is_file():
                return str(p)
    if exp.exists() and exp.is_file():
        return str(exp)
    return shutil.which(candidate)


def _managed_blender_executable() -> Optional[str]:
    current = RUNTIME_ROOT / "current"
    if not current.exists():
        return None

    if sys.platform == "darwin":
        cands = [
            current / "Blender.app" / "Contents" / "MacOS" / "Blender",
            current / "Contents" / "MacOS" / "Blender",
        ]
        for c in cands:
            if c.exists() and c.is_file():
                return str(c)
    elif os.name == "nt":
        for c in current.rglob("blender.exe"):
            if c.exists() and c.is_file():
                return str(c)
    else:
        for c in current.rglob("blender"):
            if c.exists() and c.is_file() and os.access(str(c), os.X_OK):
                return str(c)
    return None


def _runtime_download_spec() -> Optional[tuple[str, str]]:
    v = BLENDER_RUNTIME_VERSION
    parts = v.split(".")
    release_train = ".".join(parts[:2]) if len(parts) >= 2 else v
    base = f"https://download.blender.org/release/Blender{release_train}/blender-{v}"
    machine = platform.machine().lower()

    if sys.platform == "darwin":
        arch = "arm64" if "arm" in machine or "aarch64" in machine else "x64"
        name = f"blender-{v}-macos-{arch}.dmg"
        return f"{base}-macos-{arch}.dmg", name
    if os.name == "nt":
        name = f"blender-{v}-windows-x64.zip"
        return f"{base}-windows-x64.zip", name
    if sys.platform.startswith("linux"):
        arch = "arm64" if "arm" in machine or "aarch64" in machine else "x64"
        name = f"blender-{v}-linux-{arch}.tar.xz"
        return f"{base}-linux-{arch}.tar.xz", name
    return None


GITHUB_REPO = "hayhamLT/RenderMapperPro"   # for the auto-updater


def _bundled_asset(name: str) -> Optional[Path]:
    """Find a file under assets/ in the source tree or a frozen bundle."""
    roots = [Path(__file__).resolve().parent]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(getattr(sys, "_MEIPASS", "")))
    for root in roots:
        p = root / "assets" / name
        if p.exists():
            return p
    return None


def _update_token() -> str:
    """Read-only GitHub token for the auto-updater. Comes from the RMP_UPDATE_TOKEN
    env var (dev) or assets/update_token.txt baked into the build by CI (never in
    source). Empty → auto-update is simply off for this build."""
    t = os.environ.get("RMP_UPDATE_TOKEN", "").strip()
    if t:
        return t
    f = _bundled_asset("update_token.txt")
    if f is not None:
        try:
            return f.read_text(encoding="utf-8").strip()
        except Exception:
            return ""
    return ""


def _find_c4dpy() -> str:
    """Locate Cinema 4D's headless Python (c4dpy) for the C4D/Redshift backend."""
    import glob
    if sys.platform == "darwin":
        cands = glob.glob("/Applications/Maxon Cinema 4D */c4dpy.app/Contents/MacOS/c4dpy")
    elif os.name == "nt":
        cands = glob.glob(r"C:\Program Files\Maxon Cinema 4D *\c4dpy.exe")
    else:
        cands = glob.glob("/opt/maxon/*/c4dpy")
    return sorted(cands, reverse=True)[0] if cands else ""


def _find_blender(preferred: str = "") -> Optional[str]:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(v: Optional[str]) -> None:
        x = (v or "").strip()
        if x and x not in seen:
            seen.add(x)
            candidates.append(x)

    add(preferred)
    add(_managed_blender_executable())
    add(os.environ.get("BLENDER_PATH"))
    add(shutil.which("blender"))
    add("blender")
    if sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            if root.exists():
                for bundle in sorted(root.glob("Blender*.app"), reverse=True):
                    add(str(bundle))
                    add(str(bundle / "Contents/MacOS/Blender"))
    elif os.name == "nt":
        import glob as _glob
        for pat in (r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
                    r"C:\Program Files\Blender Foundation\*\blender.exe",
                    r"C:\Program Files\Blender Foundation\blender.exe",
                    r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe"):
            for hit in sorted(_glob.glob(pat), reverse=True):
                add(hit)
    else:  # linux
        import glob as _glob
        for p in ("/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender"):
            add(p)
        for hit in sorted(_glob.glob("/opt/blender*/blender"), reverse=True):
            add(hit)

    for c in candidates:
        r = _norm_blender(c)
        if r:
            return r
    return None


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
        subprocess.Popen(["open", "-R", str(p)])
    elif os.name == "nt":
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p.parent)])


# Common system install locations to check after PATH (GUI apps launched from
# Finder often have a minimal PATH that misses Homebrew / MacPorts).
_FFMPEG_SYS_DIRS = [
    "/opt/homebrew/bin",   # Apple-silicon Homebrew
    "/usr/local/bin",      # Intel Homebrew
    "/usr/bin",
    "/opt/local/bin",      # MacPorts
    r"C:\ffmpeg\bin",
]

_ffmpeg_tool_cache: dict[str, Optional[str]] = {}


def find_ffmpeg_tool(name: str) -> Optional[str]:
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

    resolved: Optional[str] = None
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
            c = os.path.join(d, exe)
            if os.path.exists(c):
                resolved = c
                break

    _ffmpeg_tool_cache[name] = resolved
    return resolved


def _find_ffprobe() -> Optional[str]:
    return find_ffmpeg_tool("ffprobe")


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


def _parse_mp4_info(path: str) -> Optional[tuple[int, float]]:
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


def _normalize_fps(raw: Optional[float], default: int = 30) -> int:
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


def _extract_frame(line: str) -> Optional[int]:
    for p in FRAME_RE:
        m = p.search(line)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                return None
    return None


def _resolve_runtime_script(name: str) -> str:
    roots = [Path(__file__).parent, Path.cwd()]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(getattr(sys, "_MEIPASS", "")))
    for root in roots:
        c = root / name
        if c.exists() and c.is_file():
            return str(c)
    raise FileNotFoundError(f"Runtime script not found: {name}")


@dataclass
class RenderJob:
    id: int
    video_path: str = ""
    label: str = ""
    custom_label: bool = False   # True once the user renames the job by hand
    output_path: str = ""
    output_input: str = ""
    scene_path: str = ""
    target_camera: str = ""
    output_profile: str = "H264 MP4"
    render_options: Optional[RenderOptions] = None
    safe_mode: bool = True
    status: str = "idle"
    error: str = ""
    attempts: int = 0
    progress: float = 0.0
    selected: bool = True
    use_deadline: bool = False
    deadline_pool: str = ""
    deadline_secondary_pool: str = ""
    deadline_group: str = ""
    deadline_priority: int = 50
    deadline_comment: str = ""
    deadline_department: str = ""
    deadline_chunk_size: int = 1
    deadline_suspended: bool = False
    deadline_job_name_template: str = "BlenderRender Job - {scene_name}"
    deadline_machine_limit: int = 0
    deadline_limits: str = ""
    deadline_command_path: str = ""
    deadline_repo_path: str = ""
    deadline_whitelist: str = ""
    deadline_submit_scene: bool = True
    material_assignments: list[MaterialVideoAssignment] = field(default_factory=list)


class DiscoveryThread(QThread):
    discovered = Signal(list, list, dict)
    error = Signal(str)
    log = Signal(str)

    def __init__(self, blender: str, script: str, scene: str,
                 c4dpy: str = "", c4d_script: str = "") -> None:
        super().__init__()
        self.blender = blender
        self.script = script
        self.scene = scene
        self.c4dpy = c4dpy
        self.c4d_script = c4d_script

    def run(self) -> None:
        try:
            mats, cams, settings = discover_scene_elements(
                blender_executable=self.blender,
                discovery_script_path=self.script,
                scene_path=self.scene,
                on_log=self.log.emit,
                hard_timeout_seconds=DISCOVERY_TIMEOUT,
                c4dpy_executable=self.c4dpy,
                c4d_discover_script=self.c4d_script,
            )
            self.discovered.emit(mats, cams, settings)
        except Exception as exc:
            self.error.emit(str(exc))


class RenderThread(QThread):
    log = Signal(str)
    job_update = Signal(int, str, float)
    job_error = Signal(int, str)
    all_done = Signal()

    def __init__(self, blender: str, worker: str, entries: list[dict],
                 c4dpy: str = "", c4d_worker: str = "") -> None:
        super().__init__()
        self.blender = blender
        self.worker = worker
        self.entries = entries
        self.c4dpy = c4dpy
        self.c4d_worker = c4d_worker
        self._cancel = False
        self._skip_current = False

    def request_cancel(self) -> None:
        self._cancel = True
        self._skip_current = True

    def request_skip(self) -> None:
        """Skip only the currently running job; continue with remaining."""
        self._skip_current = True

    def run(self) -> None:
        for entry in self.entries:
            jid: int = entry["id"]
            cfg: JobConfig = entry["cfg"]

            if self._cancel:
                self.job_update.emit(jid, "cancelled", 0.0)
                continue

            self.job_update.emit(jid, "running", 0.0)
            self.log.emit(f"[app] Job {jid}: {entry.get('label', '')}")

            fs, fe = cfg.render.frame_start, cfg.render.frame_end
            span = max(1, fe - fs + 1)
            last_error: list[str] = []

            def on_log(line: str, _j: int = jid, _fs: int = fs, _span: int = span, _err: list = last_error) -> None:
                self.log.emit(line)
                low = line.lower()
                if "error" in low or "traceback" in low or "not found" in low:
                    _err.append(line.strip())
                frame = _extract_frame(line)
                if frame is not None:
                    pct = max(0.0, min(100.0, ((frame - _fs) / _span) * 100.0))
                    self.job_update.emit(_j, "running", pct)

            try:
                if getattr(cfg, "use_deadline", False):
                    rc = submit_deadline_job(
                        blender_executable=self.blender,
                        worker_script_path=self.worker,
                        job=cfg,
                        on_log=on_log,
                        c4dpy_executable=self.c4dpy,
                        c4d_worker_script=self.c4d_worker,
                    )
                else:
                    rc = run_blender_job(
                        blender_executable=self.blender,
                        worker_script_path=self.worker,
                        job=cfg,
                        on_log=on_log,
                        should_cancel=lambda: self._skip_current,
                        c4dpy_executable=self.c4dpy,
                        c4d_worker_script=self.c4d_worker,
                    )
            except Exception as exc:
                self.log.emit(f"[app] ERROR job {jid}: {exc}")
                self.job_error.emit(jid, str(exc))
                self.job_update.emit(jid, "failed", 0.0)
                self._skip_current = False
                continue

            if self._cancel or self._skip_current:
                self.job_update.emit(jid, "cancelled", 0.0)
            elif rc == 0:
                self.job_update.emit(jid, "success", 100.0)
            else:
                reason = last_error[-1] if last_error else f"Blender exited with code {rc}"
                self.job_error.emit(jid, reason)
                self.job_update.emit(jid, "failed", 0.0)
            self._skip_current = self._cancel  # reset per-job flag unless full cancel

        self.all_done.emit()


class PreviewFrameThread(QThread):
    """Renders a single frame with the current mappings to a temp PNG."""

    log = Signal(str)
    done = Signal(str, str)  # image_path, error

    def __init__(self, blender: str, worker: str, job: JobConfig, out_dir: str,
                 c4dpy: str = "", c4d_worker: str = "") -> None:
        super().__init__()
        self.blender = blender
        self.worker = worker
        self.job = job
        self.out_dir = out_dir
        self.c4dpy = c4dpy
        self.c4d_worker = c4d_worker

    def run(self) -> None:
        try:
            rc = run_blender_job(self.blender, self.worker, self.job, on_log=self.log.emit,
                                 c4dpy_executable=self.c4dpy, c4d_worker_script=self.c4d_worker)
            import glob
            pngs = sorted(glob.glob(os.path.join(self.out_dir, "*.png")))
            if rc == 0 and pngs:
                self.done.emit(pngs[-1], "")
            else:
                self.done.emit("", f"Preview render failed (exit {rc})")
        except Exception as exc:
            self.done.emit("", str(exc))


class ExportBlendThread(QThread):
    """Runs the worker in prepare mode to bake a standalone .blend (video mapping
    + render settings) for a render farm, instead of rendering."""

    log = Signal(str)
    done = Signal(bool, str)  # ok, path-or-error

    def __init__(self, blender: str, worker: str, job: JobConfig, out_path: str) -> None:
        super().__init__()
        self.blender = blender
        self.worker = worker
        self.job = job
        self.out_path = out_path

    def run(self) -> None:
        try:
            rc = run_blender_job(self.blender, self.worker, self.job, on_log=self.log.emit)
            if rc == 0 and os.path.exists(self.out_path):
                self.done.emit(True, self.out_path)
            else:
                self.done.emit(False, f"Export failed (exit {rc})")
        except Exception as exc:
            self.done.emit(False, str(exc))


class RuntimeInstallThread(QThread):
    log = Signal(str)
    finished_install = Signal(str, str)

    def _download(self, url: str, dest: Path) -> None:
        self.log.emit(f"[runtime] Downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "RenderMapperPro/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length", "0") or "0")
            read = 0
            while True:
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                out.write(chunk)
                read += len(chunk)
                if total > 0:
                    pct = int((read / total) * 100)
                    self.log.emit(f"[runtime] Download {pct}%")

    def _extract_archive(self, archive_path: Path, staging_dir: Path) -> None:
        name = archive_path.name.lower()
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(staging_dir)
            return
        if name.endswith(".tar.xz"):
            with tarfile.open(archive_path, "r:xz") as tf:
                tf.extractall(staging_dir)
            return
        if name.endswith(".dmg") and sys.platform == "darwin":
            # Try hdiutil mount/copy; fall back to treating as zip if unavailable
            if not shutil.which("hdiutil"):
                raise RuntimeError("hdiutil not found — cannot mount .dmg. Try installing via Homebrew or download a .zip build.")
            mount = ""
            try:
                result = subprocess.run(
                    ["hdiutil", "attach", "-nobrowse", "-readonly", str(archive_path)],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"hdiutil attach failed: {result.stderr.strip()}")
                for ln in result.stdout.splitlines():
                    if "/Volumes/" in ln:
                        parts = ln.split("\t")
                        mount = parts[-1].strip() if parts else ""
                if not mount:
                    raise RuntimeError("Could not determine mount point from hdiutil output")

                apps = sorted(Path(mount).glob("*.app"))
                if not apps:
                    raise RuntimeError("Blender.app not found in downloaded disk image")
                self.log.emit(f"[runtime] Copying {apps[0].name} from mounted image")
                shutil.copytree(apps[0], staging_dir / "Blender.app", dirs_exist_ok=True)
            finally:
                if mount:
                    subprocess.run(["hdiutil", "detach", mount, "-force"],
                                   capture_output=True, timeout=30)
            return
        raise RuntimeError(f"Unsupported runtime archive format: {archive_path.name}")

    def _locate_executable(self, root: Path) -> str:
        if sys.platform == "darwin":
            cands = [
                root / "Blender.app" / "Contents" / "MacOS" / "Blender",
            ]
            for c in cands:
                if c.exists() and c.is_file():
                    return str(c)
            for c in root.rglob("Blender.app"):
                exe = c / "Contents" / "MacOS" / "Blender"
                if exe.exists() and exe.is_file():
                    return str(exe)
        elif os.name == "nt":
            for c in root.rglob("blender.exe"):
                if c.exists() and c.is_file():
                    return str(c)
        else:
            for c in root.rglob("blender"):
                if c.exists() and c.is_file() and os.access(str(c), os.X_OK):
                    return str(c)
        raise RuntimeError("Installed runtime executable not found")

    def run(self) -> None:
        try:
            spec = _runtime_download_spec()
            if not spec:
                raise RuntimeError("Managed runtime is not supported on this OS")
            url, archive_name = spec

            RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
            downloads = RUNTIME_ROOT / "downloads"
            downloads.mkdir(parents=True, exist_ok=True)

            archive_path = downloads / archive_name
            if not archive_path.exists() or archive_path.stat().st_size == 0:
                self._download(url, archive_path)
            else:
                self.log.emit("[runtime] Using cached runtime archive")

            with tempfile.TemporaryDirectory(prefix="blender-runtime-") as td:
                staging_dir = Path(td) / "staging"
                staging_dir.mkdir(parents=True, exist_ok=True)
                self.log.emit("[runtime] Installing runtime files")
                self._extract_archive(archive_path, staging_dir)

                exe = self._locate_executable(staging_dir)
                exe_path = Path(exe)

                final_dir = RUNTIME_ROOT / "current"
                old_dir = RUNTIME_ROOT / "previous"
                tmp_dir = RUNTIME_ROOT / ".next"
                if tmp_dir.exists():
                    shutil.rmtree(tmp_dir, ignore_errors=True)

                root_source = staging_dir
                if exe_path.is_relative_to(staging_dir):
                    parts = exe_path.relative_to(staging_dir).parts
                    if "Blender.app" in parts:
                        root_source = staging_dir / "Blender.app"
                shutil.copytree(root_source, tmp_dir, dirs_exist_ok=True)

                if old_dir.exists():
                    shutil.rmtree(old_dir, ignore_errors=True)
                if final_dir.exists():
                    final_dir.rename(old_dir)
                tmp_dir.rename(final_dir)

            installed = _managed_blender_executable()
            if not installed:
                raise RuntimeError("Runtime install completed but executable was not detected")
            self.finished_install.emit(installed, "")
        except Exception as exc:
            self.finished_install.emit("", str(exc))


# Item-data roles for the material / video lists.
ROLE_VIDEO_PATH = Qt.UserRole          # absolute video path (existing)
ROLE_HAS_AUDIO = Qt.UserRole + 1       # bool: clip carries an audio stream
ROLE_MUTED = Qt.UserRole + 2           # bool: user muted this clip's audio
ROLE_MAP_COLOR = Qt.UserRole + 3       # str hex: mapping colour, or None

_AUDIO_BADGE_PX = 14                    # logical size of the speaker glyph
_AUDIO_BADGE_MARGIN = 6                # inset from the left row edge — clears the 3px stripe (ends at x≈5)
_AUDIO_TEXT_INDENT = 17               # fixed slot reserved on every row so text always aligns


class MappingStripeDelegate(QStyledItemDelegate):
    """Base list delegate that draws a thin colour stripe at the left edge for
    a mapped row (ROLE_MAP_COLOR) — it sits in the row's left margin so the
    text never shifts and stays aligned with unmapped rows. Also washes the row
    in accent when ``panel`` reports it as cross-highlighted (the partner of the
    hovered/selected row in the other list)."""

    _STRIPE_W = 3

    def __init__(self, panel, kind: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._panel = panel
        self._kind = kind  # "material" | "video"

    def _item_key(self, index):
        if self._kind == "video":
            return index.data(ROLE_VIDEO_PATH)
        return index.data(Qt.DisplayRole)

    def _paint_cross_highlight(self, painter, option, index) -> None:
        panel = self._panel
        if panel is not None and panel._is_cross_highlighted(self._kind, self._item_key(index)):
            c = QColor(active_palette().accent)
            c.setAlpha(46)
            painter.save()
            painter.fillRect(option.rect, c)
            painter.restore()

    def _paint_stripe(self, painter, option, index) -> None:
        color = index.data(ROLE_MAP_COLOR)
        if not color:
            return
        r = option.rect
        bar = QRect(r.left() + 2, r.top() + 4, self._STRIPE_W, max(0, r.height() - 8))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawRoundedRect(bar, 1.5, 1.5)
        painter.restore()

    def paint(self, painter, option, index) -> None:  # type: ignore[override]
        self._paint_cross_highlight(painter, option, index)
        super().paint(painter, option, index)
        self._paint_stripe(painter, option, index)


class AudioBadgeDelegate(MappingStripeDelegate):
    """Video-list delegate: the mapping stripe plus a clickable speaker badge on
    the left of any row whose clip has audio. Clicking the badge toggles that
    clip's mute state; a muted clip shows a struck-through speaker."""

    def __init__(self, toggle_cb, panel, parent: Optional[QWidget] = None) -> None:
        super().__init__(panel, "video", parent)
        self._toggle = toggle_cb
        # Path of the row whose badge the cursor is currently over, set by the
        # owning VideoListWidget so paint() can give it a hover affordance.
        self._hover_badge: Optional[str] = None

    @staticmethod
    def _badge_rect(item_rect: QRect) -> QRect:
        size = _AUDIO_BADGE_PX
        x = item_rect.left() + _AUDIO_BADGE_MARGIN
        y = item_rect.center().y() - size // 2 + 1
        return QRect(x, y, size, size)

    def _paint_row_background(self, painter, option) -> None:
        """Fill the full row width with the hover / selection colour so the band
        sits *behind* the badge slot too — the default delegate would only paint
        from the indented text edge, leaving the badge floating outside it."""
        st = option.state
        pal = active_palette()
        if st & QStyle.State_Selected:
            color = QColor(pal.selection)
        elif st & QStyle.State_MouseOver:
            color = QColor(pal.surface_hover)
        else:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(option.rect, T.RADIUS_SM, T.RADIUS_SM)
        painter.restore()

    def paint(self, painter, option, index) -> None:  # type: ignore[override]
        self._paint_cross_highlight(painter, option, index)
        # Paint the hover/selection band across the whole row first so it reads
        # as one interactive strip (badge included), then draw the label in the
        # indented text slot. All rows share the same indent so text aligns
        # whether or not a badge is present.
        self._paint_row_background(painter, option)
        has_audio = bool(index.data(ROLE_HAS_AUDIO))
        opt = QStyleOptionViewItem(option)
        opt.rect = QRect(option.rect)
        opt.rect.setLeft(opt.rect.left() + _AUDIO_TEXT_INDENT)
        QStyledItemDelegate.paint(self, painter, opt, index)
        self._paint_stripe(painter, option, index)
        if has_audio:
            muted = bool(index.data(ROLE_MUTED))
            pal = active_palette()
            badge_r = self._badge_rect(option.rect)
            hovered = self._hover_badge is not None and self._hover_badge == index.data(ROLE_VIDEO_PATH)
            if hovered:
                # Accent-tinted chip behind the speaker so it clearly reads as a
                # clickable toggle on hover.
                chip = QColor(pal.accent)
                chip.setAlpha(60)
                painter.save()
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.setPen(Qt.NoPen)
                painter.setBrush(chip)
                painter.drawRoundedRect(badge_r.adjusted(-3, -2, 3, 2), 5, 5)
                painter.restore()
            if muted:
                color = pal.text_muted if hovered else pal.text_faint
            else:
                color = pal.accent
            pm = icons.pixmap("volume_x" if muted else "volume", color, _AUDIO_BADGE_PX)
            painter.drawPixmap(badge_r.topLeft(), pm)

    def editorEvent(self, event, model, option, index) -> bool:  # type: ignore[override]
        # Intercept press/release/double-click on the badge so the click toggles
        # mute without the list treating it as a (de)selection of the row.
        badge_events = (
            QEvent.MouseButtonPress,
            QEvent.MouseButtonRelease,
            QEvent.MouseButtonDblClick,
        )
        if (
            bool(index.data(ROLE_HAS_AUDIO))
            and event.type() in badge_events
            and event.button() == Qt.LeftButton
            and self._badge_rect(option.rect).contains(event.position().toPoint())
        ):
            if event.type() == QEvent.MouseButtonRelease:
                path = index.data(ROLE_VIDEO_PATH)
                if path and self._toggle:
                    self._toggle(path)
            return True  # swallow press/dblclick too → selection is untouched
        return super().editorEvent(event, model, option, index)


class _ImageView(QWidget):
    """Paints a pixmap directly in paintEvent over an opaque background — bypasses
    a Qt quirk where a QLabel's pixmap (or a translucent widget) can fail to
    composite while a global stylesheet is active."""

    def __init__(self, pixmap: QPixmap, bg: Optional[str] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pm = pixmap
        self._bg = QColor(bg) if bg else None
        # Size to the device-independent (logical) size so a Retina/2x pixmap
        # isn't drawn into an oversized box (which would push it off-centre).
        self.setFixedSize(pixmap.deviceIndependentSize().toSize())

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        if self._bg is not None:
            painter.fillRect(self.rect(), self._bg)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(0, 0, self._pm)


class VideoListWidget(QListWidget):
    files_dropped = Signal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Disable Qt's own drag-drop handling so external Finder drops
        # reach our custom event filter on the viewport instead.
        self.setDragDropMode(QAbstractItemView.NoDragDrop)
        self.setAcceptDrops(False)
        self.setMouseTracking(True)
        vp = self.viewport()
        vp.setMouseTracking(True)
        vp.setAcceptDrops(True)
        vp.installEventFilter(self)

    def _update_badge_hover(self, pos) -> None:
        """Track whether the cursor is over an audio badge and tell the delegate
        so it can paint the hover affordance; also swap to a pointing cursor."""
        deleg = self.itemDelegate()
        if not isinstance(deleg, AudioBadgeDelegate):
            return
        new_path = None
        idx = self.indexAt(pos)
        if idx.isValid() and bool(idx.data(ROLE_HAS_AUDIO)):
            if AudioBadgeDelegate._badge_rect(self.visualRect(idx)).contains(pos):
                new_path = idx.data(ROLE_VIDEO_PATH)
        if new_path != deleg._hover_badge:
            deleg._hover_badge = new_path
            self.viewport().setCursor(Qt.PointingHandCursor if new_path else Qt.ArrowCursor)
            self.viewport().update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor(active_palette().text_faint))
            painter.drawText(
                self.viewport().rect().adjusted(12, 0, -12, 0),
                Qt.AlignCenter | Qt.TextWordWrap,
                "Drag & drop videos or images here\n(or click Add)",
            )
            painter.end()

    @staticmethod
    def _paths_from_event(event) -> list[str]:
        md = event.mimeData() if hasattr(event, "mimeData") else None
        if not md:
            return []
        paths: list[str] = []
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    p = u.toLocalFile()
                    if p:
                        paths.append(p)
        if not paths and md.hasText():
            for raw in md.text().splitlines():
                s = raw.strip().lstrip("file://").replace("%20", " ")
                if os.path.isabs(s):
                    paths.append(s)
        return paths

    def eventFilter(self, watched, event):  # type: ignore[override]
        try:
            vp = super().viewport()
        except Exception:
            return super().eventFilter(watched, event)
        if watched is vp:
            t = event.type()
            if t in (QEvent.DragEnter, QEvent.DragMove):
                if self._paths_from_event(event):
                    event.acceptProposedAction()
                    return True
            elif t == QEvent.Drop:
                paths = self._paths_from_event(event)
                if paths:
                    self.files_dropped.emit(paths)
                    event.acceptProposedAction()
                    return True
            elif t == QEvent.MouseMove:
                self._update_badge_hover(event.position().toPoint())
            elif t == QEvent.Leave:
                self._update_badge_hover(QPoint(-1, -1))
        return super().eventFilter(watched, event)


class ScenePathLineEdit(QLineEdit):
    file_dropped = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _extract_scene_file_path(event) -> str:
        md = event.mimeData()
        if not md or not md.hasUrls():
            return ""
        for u in md.urls():
            if not u.isLocalFile():
                continue
            p = Path(u.toLocalFile()).expanduser()
            if p.suffix.lower() in SCENE_EXTENSIONS:
                return str(p)
        return ""

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if self._extract_scene_file_path(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._extract_scene_file_path(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        path = self._extract_scene_file_path(event)
        if path:
            self.file_dropped.emit(path)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class MaterialListWidget(QListWidget):
    """Materials list that also accepts a dropped scene file and shows a
    drag-drop hint while empty."""

    scene_dropped = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _scene_path(event) -> str:
        md = event.mimeData()
        if not md or not md.hasUrls():
            return ""
        for u in md.urls():
            if u.isLocalFile():
                p = Path(u.toLocalFile())
                if p.suffix.lower() in SCENE_EXTENSIONS:
                    return str(p)
        return ""

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if self._scene_path(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._scene_path(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        path = self._scene_path(event)
        if path:
            self.scene_dropped.emit(path)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor(active_palette().text_faint))
            painter.drawText(
                self.viewport().rect().adjusted(12, 0, -12, 0),
                Qt.AlignCenter | Qt.TextWordWrap,
                "Drag & drop your scene here\n(.glb, .blend, .fbx…)",
            )
            painter.end()


class ScenePanel(QWidget):
    scan_requested = Signal()
    videos_changed = Signal(list)
    assignments_changed = Signal(list)
    render_requested = Signal()
    recent_scene_selected = Signal(str)
    mute_changed = Signal()
    auto_mapped = Signal(int, int)   # (matched, total materials)
    watch_status = Signal(str)       # log message from the watch folder
    watch_changed = Signal(str, bool)  # (folder, enabled) — for persistence
    _watch_scanned = Signal(list)    # internal: background scan results → UI thread
    target_set_ready = Signal(list)  # mapped set changed + settled → auto-render
    assignments_cleared = Signal(list)  # mappings about to be cleared (for undo)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._materials: list[str] = []
        self._videos: list[str] = []
        self._assignments: list[MaterialVideoAssignment] = []
        self._recent_scenes: list[str] = []
        self._muted_videos: set[str] = set()
        # Cross-highlight: partner rows lit up for the hovered/selected row.
        self._hl_materials: set[str] = set()
        self._hl_videos: set[str] = set()
        self._hover_material: Optional[str] = None
        self._hover_video: Optional[str] = None
        self._watch_folder: str = ""
        self._watch_seen: dict[str, float] = {}
        self._watch_sizes: dict[str, int] = {}   # last-seen size, for write-in-progress detection
        self._watch_scanning = False             # a background scan is in flight
        self._watch_interval_ms = 3000           # poll cadence (configurable in Properties)
        self._watch_settle = 2.0                 # seconds a file must be quiet before ingest
        self._watch_ignore = ""                  # a dir to skip while scanning (auto-render output)
        self._autorender_last = None             # last mapped version-set emitted
        self._build_ui()
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(self._watch_interval_ms)   # poll (robust on network shares)
        self._watch_timer.timeout.connect(self._scan_watch_folder)
        self._watch_scanned.connect(self._apply_watch_scan)
        # Debounce auto-render: coalesce a burst of new target versions (landing
        # across several polls) into a single render once the set settles.
        self._autorender_timer = QTimer(self)
        self._autorender_timer.setSingleShot(True)
        self._autorender_timer.setInterval(max(4000, 2 * self._watch_interval_ms))
        self._autorender_timer.timeout.connect(self._fire_target_set)
        # Any mapping change (manual link, auto-map, unmap, watch ingest) re-checks
        # the auto-render set — mapping a clip is what targets a material now.
        self.assignments_changed.connect(lambda *_: self._check_target_set())

    # ── Cross-highlight between the material and video lists ──────────────
    def _is_cross_highlighted(self, kind: str, key) -> bool:
        if not key:
            return False
        return key in (self._hl_materials if kind == "material" else self._hl_videos)

    def _on_mat_entered(self, item) -> None:
        self._hover_material = item.text() if item else None
        self._recompute_cross_highlight()

    def _on_vid_entered(self, item) -> None:
        self._hover_video = item.data(ROLE_VIDEO_PATH) if item else None
        self._recompute_cross_highlight()

    def _recompute_cross_highlight(self) -> None:
        focus_mat = self._hover_material or self.current_material()
        focus_vid = self._hover_video or self.current_video()
        hl_vid = {a.video_path for a in self._assignments if a.material_name == focus_mat} if focus_mat else set()
        hl_mat = {a.material_name for a in self._assignments if a.video_path == focus_vid} if focus_vid else set()
        if hl_vid == self._hl_videos and hl_mat == self._hl_materials:
            return
        self._hl_videos, self._hl_materials = hl_vid, hl_mat
        self.mat_list.viewport().update()
        self.vid_list.viewport().update()

    def eventFilter(self, obj, event):  # type: ignore[override]
        # Clear the hovered row when the cursor leaves a list, so the
        # cross-highlight falls back to the selection.
        if event.type() == QEvent.Leave:
            if obj is self.mat_list.viewport() and self._hover_material is not None:
                self._hover_material = None
                self._recompute_cross_highlight()
            elif obj is self.vid_list.viewport() and self._hover_video is not None:
                self._hover_video = None
                self._recompute_cross_highlight()
        return super().eventFilter(obj, event)

    # ── Per-clip audio muting ────────────────────────────────────────────
    def is_muted(self, path: str) -> bool:
        return path in self._muted_videos

    def toggle_mute(self, path: str) -> None:
        if not path:
            return
        if path in self._muted_videos:
            self._muted_videos.discard(path)
        else:
            self._muted_videos.add(path)
        self._refresh_lists()
        self.mute_changed.emit()

    def get_muted_videos(self) -> list[str]:
        return [v for v in self._videos if v in self._muted_videos]

    def set_muted_videos(self, paths: list[str]) -> None:
        self._muted_videos = {p for p in paths if p}
        self._refresh_lists()

    def set_recent_scenes(self, scenes: list[str]) -> None:
        self._recent_scenes = [s for s in scenes if s]
        self.recent_btn.setEnabled(bool(self._recent_scenes))

    def _show_recent_menu(self) -> None:
        menu = QMenu(self)
        if not self._recent_scenes:
            act = menu.addAction("No recent scenes")
            act.setEnabled(False)
        else:
            for path in self._recent_scenes:
                act = menu.addAction(Path(path).name)
                act.setToolTip(path)
                act.triggered.connect(lambda _c=False, p=path: self.recent_scene_selected.emit(p))
        menu.exec(self.recent_btn.mapToGlobal(self.recent_btn.rect().bottomLeft()))

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # Scene path gets its own full-width row so long paths are readable
        # in the narrow dock; the actions sit on a row beneath it.
        scene_path_row = QHBoxLayout()
        scene_path_row.setSpacing(6)
        self.scene_edit = ScenePathLineEdit()
        self.scene_edit.setPlaceholderText("Drop or pick a 3D scene file (.blend, .glb, .fbx…)")
        self.scene_edit.setMinimumHeight(32)
        self.scene_edit.file_dropped.connect(self._on_scene_file_dropped)
        self.scene_edit.textChanged.connect(
            lambda t: self.scene_edit.setToolTip(t.strip() or "")
        )
        self.recent_btn = QPushButton("")
        self.recent_btn.setObjectName("IconButton")
        self.recent_btn.setToolTip("Recently opened scenes")
        self.recent_btn.setFixedSize(32, 32)
        self.recent_btn.clicked.connect(self._show_recent_menu)
        scene_path_row.addWidget(self.scene_edit, 1)
        scene_path_row.addWidget(self.recent_btn)
        root.addLayout(scene_path_row)
        self.recent_scene_selected.connect(self._on_scene_file_dropped)

        scene_btn_row = QHBoxLayout()
        scene_btn_row.setSpacing(6)
        self.browse_scene_btn = QPushButton("")
        self.browse_scene_btn.setObjectName("PrimaryButton")
        self.browse_scene_btn.setToolTip("Browse for a scene file")
        self.browse_scene_btn.setFixedSize(40, 32)
        self.browse_scene_btn.clicked.connect(self._browse_scene)
        self.scan_btn = QPushButton("")
        self.scan_btn.setObjectName("IconButton")
        self.scan_btn.setToolTip("Rescan scene")
        self.scan_btn.setFixedSize(36, 32)
        self.scan_btn.clicked.connect(self.scan_requested.emit)
        scene_btn_row.addWidget(self.browse_scene_btn)
        scene_btn_row.addWidget(self.scan_btn)
        scene_btn_row.addStretch()
        root.addLayout(scene_btn_row)

        cam_lbl = QLabel("Camera")
        cam_lbl.setObjectName("FieldLabel")
        camera_row = QHBoxLayout()
        self.camera_combo = QComboBox()
        camera_row.addWidget(cam_lbl)
        camera_row.addWidget(self.camera_combo, 1)
        root.addLayout(camera_row)

        lists = QHBoxLayout()

        left = QVBoxLayout()
        mat_lbl = QLabel("Materials")
        mat_lbl.setObjectName("FieldLabel")
        left_top = QHBoxLayout()
        left_top.setContentsMargins(0, 0, 0, 0)
        left_top.addWidget(mat_lbl)
        left_top.addStretch()
        left_top_w = QWidget()
        left_top_w.setLayout(left_top)
        left_top_w.setFixedHeight(24)
        left.addWidget(left_top_w)
        self.mat_search = QLineEdit()
        self.mat_search.setPlaceholderText("Filter materials")
        self.mat_search.textChanged.connect(self._refresh_lists)
        self.mat_list = MaterialListWidget()
        # A mapped material shows a colour stripe — that stripe IS the "targeted"
        # indicator (mapping a clip targets the material); unmapped rows are empty.
        self.mat_list.setItemDelegate(MappingStripeDelegate(self, "material", self.mat_list))
        self.mat_list.setMouseTracking(True)
        self.mat_list.viewport().setMouseTracking(True)
        self.mat_list.currentItemChanged.connect(lambda *_: self._update_maplink_btn())
        self.mat_list.currentItemChanged.connect(lambda *_: self._recompute_cross_highlight())
        self.mat_list.itemEntered.connect(self._on_mat_entered)
        self.mat_list.viewport().installEventFilter(self)
        self.mat_list.scene_dropped.connect(self._on_scene_file_dropped)
        left.addWidget(self.mat_search)
        left.addWidget(self.mat_list)

        middle = QVBoxLayout()
        middle.addStretch()
        self.maplink_btn = QPushButton("")
        self.maplink_btn.setObjectName("IconButton")
        self.maplink_btn.setFixedSize(36, 32)
        self.maplink_btn.clicked.connect(self._toggle_map_selected)

        self.automap_btn = QPushButton("")
        self.automap_btn.setObjectName("IconButton")
        self.automap_btn.setToolTip("Auto-map clips to materials by name")
        self.automap_btn.setFixedSize(36, 32)
        self.automap_btn.clicked.connect(self._auto_map_by_name)

        self.clear_map_btn = QPushButton("")
        self.clear_map_btn.setObjectName("IconButton")
        self.clear_map_btn.setToolTip("Clear all mappings")
        self.clear_map_btn.setFixedSize(36, 32)
        self.clear_map_btn.clicked.connect(self._clear_assignments)
        middle.addWidget(self.maplink_btn)
        middle.addWidget(self.automap_btn)
        middle.addWidget(self.clear_map_btn)
        middle.addStretch()

        right = QVBoxLayout()
        right_top = QHBoxLayout()
        right_top.setContentsMargins(0, 0, 0, 0)
        vid_lbl = QLabel("Videos")
        vid_lbl.setObjectName("FieldLabel")
        right_top.addWidget(vid_lbl)
        right_top.addStretch()
        self.add_video_btn = QPushButton("")
        self.add_video_btn.setObjectName("IconButton")
        self.add_video_btn.setToolTip("Add videos")
        self.add_video_btn.setFixedSize(26, 20)
        self.add_video_btn.clicked.connect(self._add_videos)
        right_top.addWidget(self.add_video_btn)
        right_top_w = QWidget()
        right_top_w.setLayout(right_top)
        right_top_w.setFixedHeight(24)
        self.vid_search = QLineEdit()
        self.vid_search.setPlaceholderText("Filter videos")
        self.vid_search.textChanged.connect(self._refresh_lists)
        self.vid_list = VideoListWidget()
        self.vid_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.vid_list.viewport().setMouseTracking(True)
        self.vid_list.setItemDelegate(AudioBadgeDelegate(self.toggle_mute, self, self.vid_list))
        self.vid_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.vid_list.customContextMenuRequested.connect(self._show_video_context_menu)
        self.vid_list.currentItemChanged.connect(lambda *_: self._recompute_cross_highlight())
        self.vid_list.itemEntered.connect(self._on_vid_entered)
        self.vid_list.viewport().installEventFilter(self)
        self.vid_list.files_dropped.connect(self._add_video_paths)

        remove_video_action = QAction("Remove Selected", self)
        # macOS "delete" key emits Backspace; bind both so it actually fires.
        remove_video_action.setShortcuts([QKeySequence(Qt.Key_Delete), QKeySequence(Qt.Key_Backspace)])
        remove_video_action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        remove_video_action.triggered.connect(self._remove_selected_video)
        self.vid_list.addAction(remove_video_action)
        right.addWidget(right_top_w)
        right.addWidget(self.vid_search)
        right.addWidget(self.vid_list)

        # Watch folder: auto-import + version-update clips dropped into a folder.
        watch_row = QHBoxLayout()
        watch_row.setSpacing(4)
        self.watch_btn = QPushButton("")
        self.watch_btn.setObjectName("IconButton")
        self.watch_btn.setCheckable(True)
        self.watch_btn.setFixedSize(26, 22)
        self.watch_btn.setToolTip("Watch a folder — auto-import new clips and update to the latest version")
        self.watch_btn.toggled.connect(self._on_watch_toggled)
        self.watch_label = QLabel("No watch folder")
        self.watch_label.setObjectName("FieldLabel")
        self.watch_browse_btn = QPushButton("")
        self.watch_browse_btn.setObjectName("IconButton")
        self.watch_browse_btn.setFixedSize(26, 22)
        self.watch_browse_btn.setToolTip("Choose watch folder")
        self.watch_browse_btn.clicked.connect(self._choose_watch_folder)
        watch_row.addWidget(self.watch_btn)
        watch_row.addWidget(self.watch_label, 1)
        watch_row.addWidget(self.watch_browse_btn)
        right.addLayout(watch_row)

        lists.addLayout(left, 5)
        lists.addLayout(middle, 2)
        lists.addLayout(right, 5)
        root.addLayout(lists, 1)

        self.restyle(active_palette())

    def restyle(self, pal: T.Palette) -> None:
        """Re-tint icons for the current theme/accent."""
        c = pal.text
        self.automap_btn.setIcon(icons.icon("check_apply", c))
        self.clear_map_btn.setIcon(icons.icon("reset", c))
        self.watch_btn.setIcon(icons.icon("clock", pal.accent_text if self.watch_btn.isChecked() else c))
        self.watch_browse_btn.setIcon(icons.icon("folder", c))
        self.add_video_btn.setIcon(icons.icon("plus", c, 13))
        self.browse_scene_btn.setIcon(icons.icon("folder", pal.accent_text))
        self.scan_btn.setIcon(icons.icon("refresh", c))
        self.recent_btn.setIcon(icons.icon("chevron_down", c))
        self._update_maplink_btn()

    def _selected_material_mapped(self) -> bool:
        mat_item = self.mat_list.currentItem()
        if mat_item is None:
            return False
        material = mat_item.text()
        return any(a.material_name == material for a in self._assignments)

    def _update_maplink_btn(self) -> None:
        """Single context-aware toggle: unlink if the selected material is
        already mapped, otherwise link the selected video to it."""
        c = active_palette().text
        if self._selected_material_mapped():
            self.maplink_btn.setIcon(icons.icon("unlink", c))
            self.maplink_btn.setToolTip("Remove mapping for selected material")
        else:
            self.maplink_btn.setIcon(icons.icon("link", c))
            self.maplink_btn.setToolTip("Assign selected video to selected material")

    def _toggle_map_selected(self) -> None:
        if self._selected_material_mapped():
            self._unmap_selected_material()
        else:
            self._map_selected()

    def _browse_scene(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select 3D scene file",
            str(Path.home()),
            "3D Files (*.blend *.c4d *.fbx *.obj *.glb *.gltf *.usd *.usda *.usdc *.abc *.stl *.ply);;All Files (*)",
        )
        if path:
            self.scene_edit.setText(path)
            self.scan_requested.emit()

    def _on_scene_file_dropped(self, path: str) -> None:
        if not path:
            return
        self.scene_edit.setText(path)
        self.scan_requested.emit()

    def _add_videos(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Select video files",
            str(Path.home()),
            "Media (*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.png *.jpg *.jpeg *.tif *.tiff *.exr *.webp);;All Files (*)",
        )
        self._add_video_paths(paths)

    @staticmethod
    def _normalize_video_path(raw_path: str) -> str:
        p = (raw_path or "").strip()
        if not p:
            return ""
        if (p.startswith("{") and p.endswith("}")) or (p.startswith('"') and p.endswith('"')):
            p = p[1:-1].strip()
        if p.startswith("file://"):
            q = QUrl(p)
            local = q.toLocalFile()
            if local:
                p = local
            else:
                p = p[7:]
        elif "://" in p:
            q = QUrl(p)
            local = q.toLocalFile()
            if local:
                p = local
        return str(Path(os.path.expanduser(p)))

    def _add_video_paths(self, paths: list[str]) -> None:
        if not paths:
            return
        for raw in paths:
            p = self._normalize_video_path(raw)
            if not p or Path(p).is_dir():
                continue
            if p not in self._videos:
                self._videos.append(p)
        self.vid_search.blockSignals(True)
        self.vid_search.clear()
        self.vid_search.blockSignals(False)
        self._refresh_lists()
        self.videos_changed.emit(list(self._videos))
        # Auto-map freshly imported clips to materials by name (gap-fill only;
        # silent unless something actually matched).
        added = self._auto_map_by_name(announce=False)
        if added:
            self.auto_mapped.emit(added, len(self._materials))

    def _remove_selected_video(self) -> None:
        items = self.vid_list.selectedItems() or ([self.vid_list.currentItem()] if self.vid_list.currentItem() else [])
        paths: set[str] = set()
        for item in items:
            if item is None:
                continue
            path = item.data(Qt.UserRole) or item.text()
            if str(path).startswith("__add_video__"):
                continue
            if not os.path.isabs(path):
                for v in self._videos:
                    if Path(v).name == path:
                        path = v
                        break
            paths.add(path)
        if not paths:
            return
        self._videos = [v for v in self._videos if v not in paths]
        self._assignments = [a for a in self._assignments if a.video_path not in paths]
        self._muted_videos -= paths
        self._refresh_lists()
        self.videos_changed.emit(list(self._videos))
        self.assignments_changed.emit(list(self._assignments))

    def _map_selected(self) -> None:
        mat_item = self.mat_list.currentItem()
        vid_item = self.vid_list.currentItem()
        if mat_item is None or vid_item is None:
            return
        material = mat_item.text()
        video = vid_item.data(Qt.UserRole) or vid_item.text()
        if str(video).startswith("__add_video__"):
            return
        replacement = MaterialVideoAssignment(material, video, VIDEO_MAPPING_MODE_EMISSION)
        for i, a in enumerate(self._assignments):
            if a.material_name == material:
                self._assignments[i] = replacement
                break
        else:
            self._assignments.append(replacement)
        self._refresh_lists()
        self.assignments_changed.emit(list(self._assignments))

    def _unmap_selected_material(self) -> None:
        mat_item = self.mat_list.currentItem()
        if mat_item is None:
            return
        material = mat_item.text()
        self._assignments = [a for a in self._assignments if a.material_name != material]
        self._refresh_lists()
        self.assignments_changed.emit(list(self._assignments))

    def _clear_assignments(self) -> None:
        if not self._assignments:
            return
        self.assignments_cleared.emit(
            [MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode) for a in self._assignments])
        self._assignments = []
        self._refresh_lists()
        self.assignments_changed.emit([])

    def _auto_map_by_name(self, announce: bool = True) -> int:
        """Fill in mappings for materials whose name appears in a clip's
        filename. Gap-fill only: already-mapped materials and already-used clips
        are left as-is, so manual choices are never overwritten. Returns the
        number of new mappings added."""
        mapped_mats = {a.material_name for a in self._assignments}
        used_vids = {a.video_path for a in self._assignments}
        free_mats = [m for m in self._materials if m not in mapped_mats]
        free_vids = [v for v in self._videos if v not in used_vids]
        matches = auto_match_media_to_materials(free_mats, free_vids)
        for material, video in matches.items():
            self._assignments.append(
                MaterialVideoAssignment(material, video, VIDEO_MAPPING_MODE_EMISSION))
        if matches:
            self._refresh_lists()
            self.assignments_changed.emit(list(self._assignments))
        if announce:
            self.auto_mapped.emit(len(matches), len(self._materials))
        self._check_target_set()
        return len(matches)

    # ── Render targets (for auto-render) ─────────────────────────────────
    def _check_target_set(self) -> None:
        """Mapping a clip *is* targeting the material, so auto-render covers every
        mapped material. When that set changes, (re)start a short debounce so a
        batch of new clips/versions becomes a single render, not one per file."""
        if not self._assignments:
            self._autorender_timer.stop()       # nothing mapped → nothing to render
            return
        version_set = frozenset((a.material_name, a.video_path) for a in self._assignments)
        if version_set == self._autorender_last:
            return                              # already rendered this exact set
        self._autorender_timer.start()          # wait for the set to settle, then fire

    def _fire_target_set(self) -> None:
        if not self._assignments:
            return
        version_set = frozenset((a.material_name, a.video_path) for a in self._assignments)
        if version_set == self._autorender_last:
            return
        self._autorender_last = version_set
        # Primary (first mapping) drives the output name.
        snapshot = [MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode)
                    for a in self._assignments]
        self.target_set_ready.emit(snapshot)

    # ── Watch folder ─────────────────────────────────────────────────────
    def _choose_watch_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose watch folder",
                                             self._watch_folder or str(Path.home()))
        if d:
            self.set_watch_folder(d, True)
            self.watch_changed.emit(self._watch_folder, True)

    def _on_watch_toggled(self, on: bool) -> None:
        self._update_watch_ui()
        if on and self._watch_folder:
            self._watch_seen = {}          # force a fresh scan
            self._watch_sizes = {}
            self._scan_watch_folder()
            self._watch_timer.start()
        else:
            self._watch_timer.stop()
        self.watch_changed.emit(self._watch_folder, self.watch_btn.isChecked())

    def set_watch_folder(self, folder: str, enabled: bool) -> None:
        """Set (and optionally start) the watch folder — used on profile load."""
        self._watch_folder = folder or ""
        self.watch_btn.blockSignals(True)
        self.watch_btn.setChecked(bool(enabled and self._watch_folder))
        self.watch_btn.blockSignals(False)
        self._update_watch_ui()
        if self.watch_btn.isChecked():
            self._watch_seen = {}
            self._watch_sizes = {}
            self._scan_watch_folder()
            self._watch_timer.start()
        else:
            self._watch_timer.stop()

    def get_watch_folder(self) -> tuple[str, bool]:
        return self._watch_folder, self.watch_btn.isChecked()

    def _update_watch_ui(self) -> None:
        name = Path(self._watch_folder).name if self._watch_folder else "No watch folder"
        self.watch_label.setText(name)
        self.watch_label.setToolTip(self._watch_folder or "Choose a folder to watch")
        self.restyle(active_palette())   # the toggle's tint shows the watching state

    def set_watch_options(self, interval_ms: int, settle_s: float) -> None:
        """Poll cadence + file-stability window (set from Properties)."""
        self._watch_interval_ms = max(1000, int(interval_ms))
        self._watch_settle = max(0.0, float(settle_s))
        self._watch_timer.setInterval(self._watch_interval_ms)
        self._autorender_timer.setInterval(max(4000, 2 * self._watch_interval_ms))

    def get_watch_options(self) -> tuple[int, float]:
        return self._watch_interval_ms, self._watch_settle

    def _scan_watch_folder(self) -> None:
        """Kick off a directory listing on a worker thread (so a slow network
        share never blocks the UI); results are applied back on the UI thread."""
        folder = self._watch_folder
        if not folder or not os.path.isdir(folder) or self._watch_scanning:
            return
        self._watch_scanning = True
        exts = VIDEO_EXTENSIONS | IMAGE_MEDIA_EXTENSIONS
        ignore = os.path.normpath(self._watch_ignore) if self._watch_ignore else ""

        def work():
            listing = []
            try:
                with os.scandir(folder) as it:
                    for e in it:
                        try:
                            # Skip the auto-render output dir so rendered PREVIZ
                            # files are never re-ingested (no feedback loop).
                            if ignore and os.path.normpath(e.path) == ignore:
                                continue
                            if e.is_file() and Path(e.name).suffix.lower() in exts:
                                st = e.stat()
                                listing.append((e.path, st.st_size, st.st_mtime))
                        except OSError:
                            pass
            except OSError:
                listing = []
            self._watch_scanned.emit(listing)   # queued → delivered on the UI thread

    def set_watch_ignore_dir(self, path: str) -> None:
        self._watch_ignore = path or ""

        threading.Thread(target=work, daemon=True).start()

    def _apply_watch_scan(self, listing: list) -> None:
        self._watch_scanning = False
        if not self._watch_folder:
            return
        folder = os.path.normpath(self._watch_folder)
        now = time.time()
        present = {os.path.normpath(p) for p, _s, _m in listing}
        # Clips that came from the watch folder but are gone now (deleted, or
        # renamed away) — drop them so a rename doesn't leave a stale duplicate.
        gone = {v for v in self._videos
                if os.path.normpath(os.path.dirname(v)) == folder and os.path.normpath(v) not in present}

        ready, mtimes, sizes = [], {}, {}
        for path, size, mtime in listing:
            sizes[path] = size
            # "Ready" = finished copying: non-empty and either its size held
            # steady since the last poll, or it hasn't been touched for a while.
            # This avoids ingesting a half-written file mid-copy.
            if size > 0 and (self._watch_sizes.get(path) == size or (now - mtime) >= self._watch_settle):
                ready.append(path)
                mtimes[path] = mtime
        self._watch_sizes = sizes           # remember sizes for next poll's stability check
        sig = (dict(mtimes), tuple(sorted(gone)))
        if sig == self._watch_seen:
            return                          # nothing changed since last poll
        self._watch_seen = sig

        base_videos = [v for v in self._videos if v not in gone]
        videos_after, replacements, added = reconcile_versions(base_videos, ready, mtimes)
        if not gone and not replacements and not added:
            return

        assignments_changed = bool(replacements)
        if replacements:
            for i, a in enumerate(self._assignments):
                if a.video_path in replacements:
                    self._assignments[i] = MaterialVideoAssignment(
                        a.material_name, replacements[a.video_path], a.mapping_mode)
            for old, new in replacements.items():
                if old in self._muted_videos:
                    self._muted_videos.discard(old)
                    self._muted_videos.add(new)
        if gone:
            self._assignments = [a for a in self._assignments if a.video_path not in gone]
            self._muted_videos -= gone
            assignments_changed = True
        self._videos = videos_after
        self._refresh_lists()
        self.videos_changed.emit(list(self._videos))
        if assignments_changed:
            self.assignments_changed.emit(list(self._assignments))
        n_new = self._auto_map_by_name(announce=False) if added else 0

        parts = []
        if added:
            parts.append(f"imported {len(added)} new")
        if replacements:
            parts.append(f"updated {len(replacements)} to latest version")
        if gone:
            parts.append(f"removed {len(gone)} deleted")
        if n_new:
            parts.append(f"auto-mapped {n_new}")
        if parts:
            self.watch_status.emit("Watch folder: " + ", ".join(parts))
        self._check_target_set()        # new versions of targets → auto-render

    def _refresh_lists(self) -> None:
        mq = self.mat_search.text().strip().lower()
        vq = self.vid_search.text().strip().lower()
        mat_to_idx = {a.material_name: i for i, a in enumerate(self._assignments)}
        vid_to_idx = {a.video_path: i for i, a in enumerate(self._assignments)}

        current_mat = self.current_material()
        current_vid = self.current_video()

        self.mat_list.blockSignals(True)
        self.mat_list.clear()
        for m in self._materials:
            if mq and mq not in m.lower():
                continue
            item = QListWidgetItem(m)
            if m in mat_to_idx:
                item.setData(ROLE_MAP_COLOR, LINK_COLORS[mat_to_idx[m] % len(LINK_COLORS)])
                item.setToolTip(f"{m}\nMapped — included in renders")
            self.mat_list.addItem(item)
        if current_mat:
            for i in range(self.mat_list.count()):
                if self.mat_list.item(i).text() == current_mat:
                    self.mat_list.setCurrentRow(i)
                    break
        self.mat_list.blockSignals(False)

        self.vid_list.blockSignals(True)
        self.vid_list.clear()
        for v in self._videos:
            name = Path(v).name
            if vq and vq not in name.lower():
                continue
            item = QListWidgetItem(name)
            item.setData(ROLE_VIDEO_PATH, v)
            if v in vid_to_idx:
                item.setData(ROLE_MAP_COLOR, LINK_COLORS[vid_to_idx[v] % len(LINK_COLORS)])
            has_audio = video_has_audio(v)
            muted = v in self._muted_videos
            item.setData(ROLE_HAS_AUDIO, has_audio)
            item.setData(ROLE_MUTED, muted)
            if has_audio:
                state = "muted — click the speaker to include" if muted else "audio on — click the speaker to mute"
                item.setToolTip(f"{name}\n🔊 {state}")
            else:
                item.setToolTip(name)
            self.vid_list.addItem(item)
        if current_vid:
            for i in range(self.vid_list.count()):
                if self.vid_list.item(i).data(Qt.UserRole) == current_vid:
                    self.vid_list.setCurrentRow(i)
                    break
        self.vid_list.blockSignals(False)
        self._update_maplink_btn()
        self._recompute_cross_highlight()

    def set_materials(self, materials: list[str]) -> None:
        self._materials = materials
        self._refresh_lists()

    def set_cameras(self, cameras: list[str], selected: str = "") -> None:
        normalized = [str(c).strip() for c in cameras if str(c).strip()]
        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        self.camera_combo.addItems([""] + normalized)
        if selected:
            idx = self.camera_combo.findText(selected)
            if idx >= 0:
                self.camera_combo.setCurrentIndex(idx)
            else:
                if len(normalized) > 0:
                    self.camera_combo.setCurrentIndex(1)
                else:
                    self.camera_combo.setCurrentIndex(0)
        else:
            if len(normalized) > 0:
                self.camera_combo.setCurrentIndex(1)
            else:
                self.camera_combo.setCurrentIndex(0)
        self.camera_combo.blockSignals(False)

    def set_videos(self, videos: list[str]) -> None:
        self._videos = videos
        self._refresh_lists()

    def refresh_videos(self) -> None:
        """Re-probe the current clips (audio streams, etc.) and rebuild the list.
        Used on rescan so badges/info reflect the files on disk right now."""
        for v in self._videos:
            _audio_probe_cache.pop(v, None)
        self._refresh_lists()

    def set_assignments(self, assignments: list[MaterialVideoAssignment]) -> None:
        self._assignments = assignments
        self._refresh_lists()

    def get_videos(self) -> list[str]:
        return list(self._videos)

    def get_assignments(self) -> list[MaterialVideoAssignment]:
        return list(self._assignments)

    def current_material(self) -> str:
        item = self.mat_list.currentItem()
        return item.text() if item else ""

    def current_video(self) -> str:
        item = self.vid_list.currentItem()
        if not item:
            return ""
        return item.data(Qt.UserRole) or ""

    def _show_video_context_menu(self, pos) -> None:
        item = self.vid_list.itemAt(pos)
        menu = QMenu(self)
        add_action = menu.addAction("Add Videos...")
        render_action = menu.addAction("Render Queue")
        remove_action = menu.addAction("Remove Selected")
        mute_action = None
        if item is not None and bool(item.data(ROLE_HAS_AUDIO)):
            menu.addSeparator()
            muted = bool(item.data(ROLE_MUTED))
            mute_action = menu.addAction("Unmute audio" if muted else "Mute audio")
        if item is None or str(item.data(ROLE_VIDEO_PATH) or "").startswith("__add_video__"):
            remove_action.setEnabled(False)
        chosen = menu.exec(self.vid_list.viewport().mapToGlobal(pos))
        if chosen == add_action:
            self._add_videos()
        elif chosen == render_action:
            self.render_requested.emit()
        elif chosen == remove_action:
            self._remove_selected_video()
        elif mute_action is not None and chosen == mute_action:
            self.toggle_mute(item.data(ROLE_VIDEO_PATH))


class RenderPanel(QWidget):
    output_changed = Signal(str)
    tokens_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        # Scrollable content so this dense settings panel doesn't force a tall
        # minimum window height — the window can shrink to the 70% default.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        root = QVBoxLayout(inner)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(0)

        def section(title: str) -> QLabel:
            lbl = QLabel(title)
            lbl.setObjectName("SectionLabel")
            return lbl

        # ── Resolution & FPS ──────────────────────────────────────────
        root.addWidget(section("RESOLUTION & FRAME RATE"))
        res_row = QHBoxLayout()
        res_row.setSpacing(6)
        self.width_edit = QLineEdit("1920")
        self.width_edit.setPlaceholderText("W")
        self.height_edit = QLineEdit("1080")
        self.height_edit.setPlaceholderText("H")
        x_lbl = QLabel("×")
        x_lbl.setAlignment(Qt.AlignCenter)
        self.fps_edit = QLineEdit("30")
        self.fps_edit.setPlaceholderText("FPS")
        fps_lbl = QLabel("fps")
        fps_lbl.setAlignment(Qt.AlignVCenter)
        res_row.addWidget(self.width_edit, 3)
        res_row.addWidget(x_lbl)
        res_row.addWidget(self.height_edit, 3)
        res_row.addSpacing(10)
        res_row.addWidget(self.fps_edit, 2)
        res_row.addWidget(fps_lbl)
        root.addLayout(res_row)

        # ── Frame Range ───────────────────────────────────────────────
        root.addWidget(section("FRAME RANGE"))
        fr_row = QHBoxLayout()
        fr_row.setSpacing(6)
        self.frame_start_edit = QLineEdit("1")
        self.frame_start_edit.setPlaceholderText("Start")
        self.frame_end_edit = QLineEdit("250")
        self.frame_end_edit.setPlaceholderText("End")
        self.frame_step_edit = QLineEdit("1")
        self.frame_step_edit.setPlaceholderText("Step")
        for w, lbl in ((self.frame_start_edit, "Start"), (self.frame_end_edit, "End"), (self.frame_step_edit, "Step")):
            col = QVBoxLayout()
            col.setSpacing(2)
            l = QLabel(lbl)
            l.setObjectName("FieldLabel")
            col.addWidget(l)
            col.addWidget(w)
            fr_row.addLayout(col)
        root.addLayout(fr_row)

        # ── Renderer + Output Format (side-by-side) ─────────────────
        ro_row = QHBoxLayout()
        ro_row.setSpacing(8)

        renderer_col = QVBoxLayout()
        renderer_col.setSpacing(2)
        renderer_col.addWidget(section("RENDERER"))
        self.engine_combo = QComboBox()
        self.engine_combo.addItems(["CYCLES", "BLENDER_EEVEE"])
        renderer_col.addWidget(self.engine_combo)

        format_col = QVBoxLayout()
        format_col.setSpacing(2)
        format_col.addWidget(section("OUTPUT FORMAT"))
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(list(OUTPUT_PROFILES.keys()))
        format_col.addWidget(self.profile_combo)

        ro_row.addLayout(renderer_col, 1)
        ro_row.addLayout(format_col, 1)
        root.addLayout(ro_row)

        # ── Output Path ───────────────────────────────────────────────
        root.addWidget(section("OUTPUT PATH"))
        path_row = QHBoxLayout()
        path_row.setSpacing(6)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(f"file, folder, or tokens: {OUTPUT_TOKENS}")
        self.output_edit.textChanged.connect(self.output_changed.emit)
        self.tokens_btn = QPushButton("")
        self.tokens_btn.setObjectName("IconButton")
        self.tokens_btn.setToolTip("Insert a naming token")
        self.tokens_btn.setFixedSize(30, 30)
        self.tokens_btn.clicked.connect(self.tokens_requested.emit)
        self.browse_out_btn = QPushButton("")
        self.browse_out_btn.setObjectName("IconButton")
        self.browse_out_btn.setToolTip("Browse for output path")
        self.browse_out_btn.setFixedSize(34, 30)
        self.browse_out_btn.clicked.connect(self._browse_output)
        self.open_out_btn = QPushButton("")
        self.open_out_btn.setObjectName("IconButton")
        self.open_out_btn.setToolTip("Open output folder")
        self.open_out_btn.setFixedSize(34, 30)
        self.open_out_btn.clicked.connect(self._open_output)
        path_row.addWidget(self.output_edit, 1)
        path_row.addWidget(self.tokens_btn)
        path_row.addWidget(self.browse_out_btn)
        path_row.addWidget(self.open_out_btn)
        root.addLayout(path_row)


        # ── Advanced / Quality (collapsed by default) ──────────────────
        self.adv_toggle = QPushButton("  Advanced quality settings")
        self.adv_toggle.setCheckable(True)
        self.adv_toggle.setChecked(False)
        self.adv_toggle.setObjectName("SmallButton")
        self.adv_toggle.setCursor(Qt.PointingHandCursor)
        self.adv_toggle.toggled.connect(self._on_adv_toggled)
        root.addWidget(self.adv_toggle)

        self.adv_box = QWidget()
        adv = QVBoxLayout(self.adv_box)
        adv.setContentsMargins(0, 6, 0, 0)
        adv.setSpacing(10)

        def labeled(text: str, w: QWidget) -> QVBoxLayout:
            """A field label stacked above its input, matching the panel style."""
            col = QVBoxLayout()
            col.setSpacing(3)
            lbl = QLabel(text)
            lbl.setObjectName("FieldLabel")
            col.addWidget(lbl)
            col.addWidget(w)
            return col

        def two_col(left: QVBoxLayout, right: Optional[QVBoxLayout] = None) -> QHBoxLayout:
            """Two equal-width field columns; a missing right column leaves the
            left field at half width so stacked rows stay aligned."""
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addLayout(left, 1)
            if right is not None:
                row.addLayout(right, 1)
            else:
                row.addStretch(1)
            return row

        # ── Sampling & quality (same slot for both renderers) ────────────
        adv.addWidget(section("SAMPLING & QUALITY"))
        # Speed preset (Redshift only) — sits at the top of sampling.
        self.rs_preset_combo = QComboBox()
        self.rs_preset_combo.addItems(["Custom", "Draft (fastest)", "Balanced", "High", "Final (best)"])
        self.rs_preset_combo.setToolTip("One-click speed/quality tradeoff. Fills the fields below.")
        self.rs_preset_row = QWidget()
        _pr = QVBoxLayout(self.rs_preset_row)
        _pr.setContentsMargins(0, 0, 0, 0)
        _pr.addLayout(labeled("Speed Preset", self.rs_preset_combo))
        adv.addWidget(self.rs_preset_row)
        # Samples: 'Cycles Samples' (Blender) / 'Max Samples' + 'Min Samples' (RS).
        self.samples_edit = QLineEdit("64")
        self.samples_edit.setPlaceholderText("64")
        self.samples_label = QLabel("Cycles Samples")
        self.samples_label.setObjectName("FieldLabel")
        samples_col = QVBoxLayout()
        samples_col.setSpacing(3)
        samples_col.addWidget(self.samples_label)
        samples_col.addWidget(self.samples_edit)
        self.rs_min_samples_edit = QLineEdit("4")
        self.rs_min_box = QWidget()
        _mb = QVBoxLayout(self.rs_min_box)
        _mb.setContentsMargins(0, 0, 0, 0)
        _mb.addLayout(labeled("Min Samples", self.rs_min_samples_edit))
        samples_row = QHBoxLayout()
        samples_row.setSpacing(10)
        samples_row.addLayout(samples_col, 1)
        samples_row.addWidget(self.rs_min_box, 1)
        adv.addLayout(samples_row)
        # Noise threshold (Redshift only) — the biggest speed knob.
        self.rs_threshold_edit = QLineEdit("0.01")
        self.rs_threshold_edit.setToolTip("Adaptive noise threshold — higher renders faster (noisier).")
        self.rs_threshold_row = QWidget()
        _tr = QVBoxLayout(self.rs_threshold_row)
        _tr.setContentsMargins(0, 0, 0, 0)
        _tr.addLayout(labeled("Noise Threshold  (higher = faster)", self.rs_threshold_edit))
        adv.addWidget(self.rs_threshold_row)
        # Denoise (both) + transparent (Blender only).
        self.denoise_cb = QCheckBox("Denoise")
        self.denoise_cb.setChecked(True)
        self.transparent_cb = QCheckBox("Transparent background (alpha)")
        self.transparent_cb.setToolTip("Render with a transparent background — needs PNG/EXR/ProRes output.")
        cb_row = QHBoxLayout()
        cb_row.setSpacing(18)
        cb_row.addWidget(self.denoise_cb)
        cb_row.addWidget(self.transparent_cb)
        cb_row.addStretch(1)
        adv.addLayout(cb_row)

        # ── Output (same slot for both renderers) ────────────────────────
        adv.addWidget(section("OUTPUT"))
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["100%", "75%", "50%", "25%"])
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(["Lossless", "High", "Medium", "Low", "Lowest"])
        self.quality_combo.setCurrentText("High")
        adv.addLayout(two_col(
            labeled("Render Scale", self.scale_combo),
            labeled("Video Quality", self.quality_combo),
        ))
        self.codec_combo = QComboBox()
        self.codec_combo.addItems(["Default", "H.264", "H.265"])
        self.device_combo = QComboBox()
        self.device_combo.addItems(["Auto", "GPU", "CPU"])
        # Device is Blender-only (Redshift is GPU); wrap it so it can be hidden.
        self.device_box = QWidget()
        dev_lay = QVBoxLayout(self.device_box)
        dev_lay.setContentsMargins(0, 0, 0, 0)
        dev_lay.addLayout(labeled("Device", self.device_combo))
        out_row = QHBoxLayout()
        out_row.setSpacing(10)
        out_row.addLayout(labeled("Codec", self.codec_combo), 1)
        out_row.addWidget(self.device_box, 1)
        adv.addLayout(out_row)

        # ── Lighting & GI (Redshift only) ────────────────────────────────
        self.gi_box = QWidget()
        gi_lay = QVBoxLayout(self.gi_box)
        gi_lay.setContentsMargins(0, 0, 0, 0)
        gi_lay.setSpacing(10)
        gi_lay.addWidget(section("LIGHTING & GI"))
        self.rs_gi_cb = QCheckBox("Global illumination")
        self.rs_gi_cb.setChecked(True)
        self.rs_gi_cb.setToolTip("Turn off for flat/emissive content — a large speedup.")
        gi_lay.addWidget(self.rs_gi_cb)
        self.rs_gi_bounces_edit = QLineEdit("3")
        self.rs_ray_depth_edit = QLineEdit("6")
        self.rs_ray_depth_edit.setToolTip("Max ray trace depth — fewer bounces render faster.")
        gi_lay.addLayout(two_col(
            labeled("GI Bounces", self.rs_gi_bounces_edit),
            labeled("Max Ray Depth", self.rs_ray_depth_edit),
        ))
        adv.addWidget(self.gi_box)

        # ── Color management (Blender only) ──────────────────────────────
        self.color_box = QWidget()
        color_lay = QVBoxLayout(self.color_box)
        color_lay.setContentsMargins(0, 0, 0, 0)
        color_lay.setSpacing(10)
        color_lay.addWidget(section("COLOR MANAGEMENT"))
        self.view_transform_combo = QComboBox()
        self.view_transform_combo.addItems(["AgX", "Filmic", "Standard", "Khronos PBR Neutral", "Raw", "False Color"])
        self.view_transform_combo.setCurrentText("AgX")
        color_lay.addLayout(labeled("View Transform", self.view_transform_combo))
        self.exposure_edit = QLineEdit("0.0")
        self.gamma_edit = QLineEdit("1.0")
        color_lay.addLayout(two_col(
            labeled("Exposure", self.exposure_edit),
            labeled("Gamma", self.gamma_edit),
        ))
        adv.addWidget(self.color_box)

        # Preset wiring + initial renderer state (Blender layout by default).
        self._rs_applying = False
        self.rs_preset_combo.currentTextChanged.connect(self._apply_rs_preset)
        for w in (self.samples_edit, self.rs_min_samples_edit, self.rs_threshold_edit,
                  self.rs_gi_bounces_edit, self.rs_ray_depth_edit):
            w.textEdited.connect(self._rs_custom)
        self.rs_gi_cb.toggled.connect(lambda _v: self._rs_custom())
        self.set_renderer(False)

        self.adv_box.setVisible(False)
        root.addWidget(self.adv_box)

        root.addStretch()
        self.restyle(active_palette())

    def _on_adv_toggled(self, checked: bool) -> None:
        self.adv_box.setVisible(checked)
        arrow = "▾" if checked else "▸"
        self.adv_toggle.setText(f"{arrow}  Advanced quality settings")

    # Redshift speed/quality presets — each fills the optimization fields.
    _RS_PRESETS = {
        "Draft (fastest)": dict(mx="16", mn="1", thr="0.3", gib="1", depth="3", gi=False),
        "Balanced":        dict(mx="64", mn="4", thr="0.02", gib="3", depth="6", gi=True),
        "High":            dict(mx="128", mn="8", thr="0.01", gib="3", depth="8", gi=True),
        "Final (best)":    dict(mx="256", mn="16", thr="0.005", gib="4", depth="12", gi=True),
    }

    def _apply_rs_preset(self, name: str) -> None:
        p = self._RS_PRESETS.get(name)
        if not p:
            return
        self._rs_applying = True
        try:
            self.samples_edit.setText(p["mx"])
            self.rs_min_samples_edit.setText(p["mn"])
            self.rs_threshold_edit.setText(p["thr"])
            self.rs_gi_bounces_edit.setText(p["gib"])
            self.rs_ray_depth_edit.setText(p["depth"])
            self.rs_gi_cb.setChecked(p["gi"])
        finally:
            self._rs_applying = False

    def _rs_custom(self) -> None:
        """A manual edit to any optimization field flips the preset to Custom."""
        if self._rs_applying or self.rs_preset_combo.currentText() == "Custom":
            return
        self.rs_preset_combo.blockSignals(True)
        self.rs_preset_combo.setCurrentText("Custom")
        self.rs_preset_combo.blockSignals(False)

    def set_renderer(self, is_c4d: bool) -> None:
        """Adapt the settings to the active renderer so every visible control is
        real. Redshift: relabel samples, hide Blender-only Device + Color
        Management, show the Redshift optimization controls, and offer only
        output profiles the C4D path can produce."""
        self.samples_label.setText("Max Samples" if is_c4d else "Cycles Samples")
        # Redshift-only sampling/GI controls.
        for w in (self.rs_preset_row, self.rs_min_box, self.rs_threshold_row, self.gi_box):
            w.setVisible(is_c4d)
        # Blender-only controls.
        self.device_box.setVisible(not is_c4d)       # Redshift is GPU-only
        self.color_box.setVisible(not is_c4d)        # Blender color management
        self.transparent_cb.setVisible(not is_c4d)   # alpha not wired for the C4D path
        items = ["H264 MP4", "ProRes MOV", "PNG Sequence"] if is_c4d else list(OUTPUT_PROFILES.keys())
        existing = [self.profile_combo.itemText(i) for i in range(self.profile_combo.count())]
        if existing != items:
            cur = self.profile_combo.currentText()
            self.profile_combo.blockSignals(True)
            self.profile_combo.clear()
            self.profile_combo.addItems(items)
            idx = self.profile_combo.findText(cur)
            self.profile_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.profile_combo.blockSignals(False)

    def restyle(self, pal: T.Palette) -> None:
        self.browse_out_btn.setIcon(icons.icon("folder", pal.text))
        self.open_out_btn.setIcon(icons.icon("open", pal.text))
        self.tokens_btn.setIcon(icons.icon("chevron_down", pal.text))
        if not self.adv_toggle.isChecked():
            self.adv_toggle.setText("▸  Advanced quality settings")

    def _browse_output(self) -> None:
        from core.utils import ext_for_format
        profile = self.profile_combo.currentText()
        out_fmt, _ = OUTPUT_PROFILES.get(profile, ("MPEG4", "H264"))
        ext = ext_for_format(out_fmt)  # empty for sequences

        current = self.output_edit.text().strip()
        start_dir = str(Path(current).expanduser().parent) if current else str(Path.home())

        if not ext:
            # Image sequence → pick a folder
            folder = QFileDialog.getExistingDirectory(self, "Select Output Folder", start_dir)
            if folder:
                self.output_edit.setText(folder)
        else:
            label_map = {
                ".mp4": "MP4 Files (*.mp4)",
                ".mov": "MOV Files (*.mov)",
                ".avi": "AVI Files (*.avi)",
            }
            filt = label_map.get(ext, f"*{ext} Files (*{ext})")
            default_name = f"output{ext}"
            default_path = str(Path(start_dir) / default_name)
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Select Output File",
                default_path,
                f"{filt};;All Files (*)",
            )
            if path:
                p = Path(path)
                if p.suffix.lower() != ext:
                    path = str(p.with_suffix(ext))
                self.output_edit.setText(path)

    def _open_output(self) -> None:
        output = self.output_edit.text().strip()
        if not output:
            return
        target = Path(output).expanduser()
        target = target if target.is_dir() else target.parent
        if not target.exists():
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            elif os.name == "nt":
                os.startfile(str(target))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except Exception:
            pass

    def apply_scene_settings(self, s: dict) -> None:
        """Mirror render/timeline/colour settings read from the .blend into the
        UI. Only keys present in ``s`` are applied, so a partial probe is safe."""
        if not s:
            return
        def setnum(widget, key):
            if key in s and s[key] is not None:
                widget.setText(str(int(s[key])) if isinstance(s[key], (int, float)) else str(s[key]))
        setnum(self.width_edit, "width")
        setnum(self.height_edit, "height")
        setnum(self.fps_edit, "fps")
        setnum(self.frame_start_edit, "frame_start")
        setnum(self.frame_end_edit, "frame_end")
        setnum(self.frame_step_edit, "frame_step")
        setnum(self.samples_edit, "samples")
        if "use_denoise" in s:
            self.denoise_cb.setChecked(bool(s["use_denoise"]))
        if "film_transparent" in s:
            self.transparent_cb.setChecked(bool(s["film_transparent"]))
        # Adopt the scene's Redshift optimization settings.
        if "rs_min_samples" in s:
            self.rs_min_samples_edit.setText(str(int(s["rs_min_samples"])))
        if "rs_threshold" in s:
            self.rs_threshold_edit.setText(f"{float(s['rs_threshold']):g}")
        if "rs_gi_bounces" in s:
            self.rs_gi_bounces_edit.setText(str(int(s["rs_gi_bounces"])))
        if "rs_ray_depth" in s:
            self.rs_ray_depth_edit.setText(str(int(s["rs_ray_depth"])))
        if "rs_gi_enabled" in s:
            self.rs_gi_cb.setChecked(bool(s["rs_gi_enabled"]))
        # Engine: map any EEVEE variant (EEVEE / EEVEE_NEXT) to the combo entry.
        eng = str(s.get("engine", "")).upper()
        if eng:
            target = "CYCLES" if "CYCLES" in eng else ("BLENDER_EEVEE" if "EEVEE" in eng else "")
            i = self.engine_combo.findText(target)
            if i >= 0:
                self.engine_combo.setCurrentIndex(i)
        # Render scale → nearest preset (100/75/50/25).
        if "resolution_percentage" in s:
            pct = int(s["resolution_percentage"])
            nearest = min((100, 75, 50, 25), key=lambda p: abs(p - pct))
            self.scale_combo.setCurrentText(f"{nearest}%")
        # Colour management.
        vt = s.get("view_transform")
        if vt and self.view_transform_combo.findText(str(vt)) >= 0:
            self.view_transform_combo.setCurrentText(str(vt))
        if "exposure" in s:
            self.exposure_edit.setText(f"{float(s['exposure']):g}")
        if "gamma" in s:
            self.gamma_edit.setText(f"{float(s['gamma']):g}")

    def render_options(self) -> RenderOptions:
        def to_int(v: str, d: int) -> int:
            try:
                return int(v)
            except ValueError:
                return d

        def to_float(v: str, d: float) -> float:
            try:
                return float(v)
            except ValueError:
                return d

        out_fmt, codec = OUTPUT_PROFILES.get(self.profile_combo.currentText(), ("MPEG4", "H264"))
        quality_map = {"Lossless": "LOSSLESS", "High": "HIGH", "Medium": "MEDIUM", "Low": "LOW", "Lowest": "LOWEST"}
        codec_map = {"Default": "", "H.264": "H264", "H.265": "H265"}
        device_map = {"Auto": "AUTO", "GPU": "GPU", "CPU": "CPU"}
        return RenderOptions(
            width=to_int(self.width_edit.text(), 1920),
            height=to_int(self.height_edit.text(), 1080),
            fps=to_int(self.fps_edit.text(), 30),
            frame_start=to_int(self.frame_start_edit.text(), 1),
            frame_end=to_int(self.frame_end_edit.text(), 250),
            frame_step=max(1, to_int(self.frame_step_edit.text(), 1)),
            engine=self.engine_combo.currentText(),
            samples=to_int(self.samples_edit.text(), 64),
            use_denoise=self.denoise_cb.isChecked(),
            output_format=out_fmt,
            codec=codec,
            color_view_transform=self.view_transform_combo.currentText() or "AgX",
            color_exposure=to_float(self.exposure_edit.text(), 0.0),
            color_gamma=to_float(self.gamma_edit.text(), 1.0),
            device=device_map.get(self.device_combo.currentText(), "AUTO"),
            resolution_percentage=to_int(self.scale_combo.currentText().rstrip("%"), 100),
            film_transparent=self.transparent_cb.isChecked(),
            video_quality=quality_map.get(self.quality_combo.currentText(), "HIGH"),
            video_codec=codec_map.get(self.codec_combo.currentText(), ""),
            rs_min_samples=to_int(self.rs_min_samples_edit.text(), 4),
            rs_threshold=to_float(self.rs_threshold_edit.text(), 0.01),
            rs_gi_enabled=self.rs_gi_cb.isChecked(),
            rs_gi_bounces=to_int(self.rs_gi_bounces_edit.text(), 3),
            rs_ray_depth=to_int(self.rs_ray_depth_edit.text(), 6),
        )

    def settings_dict(self) -> dict:
        """Render-recipe (settings only) for a Preset — no scene/clips/queue."""
        o = self.render_options()
        d = dataclasses.asdict(o)
        d.pop("timeout_seconds", None)
        d.pop("idle_timeout_seconds", None)
        d.pop("output_format", None)
        d.pop("codec", None)
        d["output_profile"] = self.profile_combo.currentText()
        d["type"] = "render_preset"
        return d

    def apply_settings(self, d: dict) -> None:
        """Apply a render-recipe dict to the panel widgets (tolerant of old
        full-state presets — only the render keys are read)."""
        def setnum(edit, key):
            if key in d and d[key] != "":
                edit.setText(str(d[key]))

        setnum(self.width_edit, "width")
        setnum(self.height_edit, "height")
        setnum(self.fps_edit, "fps")
        setnum(self.frame_start_edit, "frame_start")
        setnum(self.frame_end_edit, "frame_end")
        setnum(self.frame_step_edit, "frame_step")
        setnum(self.samples_edit, "samples")
        setnum(self.exposure_edit, "color_exposure")
        setnum(self.gamma_edit, "color_gamma")
        if "engine" in d:
            i = self.engine_combo.findText(str(d["engine"]))
            if i >= 0:
                self.engine_combo.setCurrentIndex(i)
        if "output_profile" in d:
            i = self.profile_combo.findText(str(d["output_profile"]))
            if i >= 0:
                self.profile_combo.setCurrentIndex(i)
        if "color_view_transform" in d:
            self.view_transform_combo.setCurrentText(str(d["color_view_transform"]))
        if "use_denoise" in d:
            self.denoise_cb.setChecked(bool(d["use_denoise"]))
        if "film_transparent" in d:
            self.transparent_cb.setChecked(bool(d["film_transparent"]))
        if "device" in d:
            self.device_combo.setCurrentText({"AUTO": "Auto", "GPU": "GPU", "CPU": "CPU"}.get(str(d["device"]).upper(), "Auto"))
        if "resolution_percentage" in d:
            try:
                self.scale_combo.setCurrentText(f"{int(d['resolution_percentage'])}%")
            except Exception:
                pass
        if "video_quality" in d:
            self.quality_combo.setCurrentText({"LOSSLESS": "Lossless", "HIGH": "High", "MEDIUM": "Medium", "LOW": "Low", "LOWEST": "Lowest"}.get(str(d["video_quality"]).upper(), "High"))
        if "video_codec" in d:
            self.codec_combo.setCurrentText({"": "Default", "H264": "H.264", "H265": "H.265"}.get(str(d["video_codec"]).upper(), "Default"))
        setnum(self.rs_min_samples_edit, "rs_min_samples")
        setnum(self.rs_threshold_edit, "rs_threshold")
        setnum(self.rs_gi_bounces_edit, "rs_gi_bounces")
        setnum(self.rs_ray_depth_edit, "rs_ray_depth")
        if "rs_gi_enabled" in d:
            self.rs_gi_cb.setChecked(bool(d["rs_gi_enabled"]))


class DeadlinePanel(QWidget):
    settings_changed = Signal()
    test_connection_requested = Signal()
    export_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _on_changed(self, *_) -> None:
        self.settings_changed.emit()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        root = QVBoxLayout(inner)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(6)

        def section(title: str) -> QLabel:
            lbl = QLabel(title)
            lbl.setObjectName("SectionLabel")
            return lbl

        # Keep these attributes defined for backwards compatibility so nothing crashes, but not added to layout
        self.dl_cmd_edit = QLineEdit()
        self.dl_repo_edit = QLineEdit()
        self.dl_dept_edit = QLineEdit()
        self.dl_sec_pool_combo = QComboBox()
        self.dl_sec_pool_combo.setEditable(True)
        self.dl_machine_limit_spin = QSpinBox()
        self.dl_limits_edit = QLineEdit()
        self.connection_status_lbl = QLabel()
        self.test_conn_btn = QPushButton()
        self.export_files_btn = QPushButton()
        self.dl_name_template_edit = QLineEdit()
        self.dl_comment_edit = QLineEdit()

        # Enable Deadline Toggle
        self.use_dl_cb = QCheckBox("Enable Deadline Submission")
        self.use_dl_cb.setObjectName("ToggleHeader")
        self.use_dl_cb.stateChanged.connect(self._on_enable_toggled)
        self.use_dl_cb.stateChanged.connect(self._on_changed)
        root.addWidget(self.use_dl_cb)

        # Container Widget for all other settings
        self.container = QWidget()
        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(6)

        # Pools & Groups
        container_layout.addWidget(section("RENDER TARGETS & POOLS"))
        pools_layout = QFormLayout()
        pools_layout.setSpacing(6)
        pools_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.dl_pool_combo = QComboBox()
        self.dl_pool_combo.setEditable(True)
        self.dl_pool_combo.lineEdit().textChanged.connect(self._on_changed)
        pools_layout.addRow("Primary Pool:", self.dl_pool_combo)

        self.dl_group_combo = QComboBox()
        self.dl_group_combo.setEditable(True)
        self.dl_group_combo.lineEdit().textChanged.connect(self._on_changed)
        pools_layout.addRow("Group:", self.dl_group_combo)

        container_layout.addLayout(pools_layout)

        # Manual Machine Selection
        container_layout.addWidget(section("MANUAL MACHINE SELECTION"))
        self.dl_machines_list = QListWidget()
        self.dl_machines_list.setFixedHeight(120)
        self.dl_machines_list.itemChanged.connect(self._on_changed)
        container_layout.addWidget(self.dl_machines_list)

        mach_btns_layout = QHBoxLayout()
        mach_btns_layout.setSpacing(6)
        _c = active_palette().text
        self.select_all_machines_btn = QPushButton("")
        self.select_all_machines_btn.setObjectName("IconButton")
        self.select_all_machines_btn.setToolTip("Select all machines")
        self.select_all_machines_btn.setFixedSize(34, 26)
        self.select_all_machines_btn.setIcon(icons.icon("check_apply", _c))
        self.select_all_machines_btn.clicked.connect(self._select_all_machines)

        self.clear_all_machines_btn = QPushButton("")
        self.clear_all_machines_btn.setObjectName("IconButton")
        self.clear_all_machines_btn.setToolTip("Clear all machine selections")
        self.clear_all_machines_btn.setFixedSize(34, 26)
        self.clear_all_machines_btn.setIcon(icons.icon("x", _c))
        self.clear_all_machines_btn.clicked.connect(self._clear_all_machines)
        
        mach_btns_layout.addWidget(self.select_all_machines_btn)
        mach_btns_layout.addWidget(self.clear_all_machines_btn)
        mach_btns_layout.addStretch()
        container_layout.addLayout(mach_btns_layout)

        # Priorities & chunk size
        container_layout.addWidget(section("JOB CONTROLS"))
        ctrl_layout = QFormLayout()
        ctrl_layout.setSpacing(6)
        
        self.dl_prio_spin = QSpinBox()
        self.dl_prio_spin.setRange(0, 100)
        self.dl_prio_spin.setValue(50)
        self.dl_prio_spin.valueChanged.connect(self._on_changed)
        ctrl_layout.addRow("Priority (0-100):", self.dl_prio_spin)

        self.dl_chunk_spin = QSpinBox()
        self.dl_chunk_spin.setRange(1, 10000)
        self.dl_chunk_spin.setValue(1)
        self.dl_chunk_spin.setToolTip("How many frames rendered by one machine per task")
        self.dl_chunk_spin.valueChanged.connect(self._on_changed)
        ctrl_layout.addRow("Frames Per Task:", self.dl_chunk_spin)

        container_layout.addLayout(ctrl_layout)

        # Initial Status
        status_layout = QHBoxLayout()
        self.dl_suspended_cb = QCheckBox("Submit Suspended")
        self.dl_suspended_cb.setToolTip("Submit in a suspended state for review before rendering starts")
        self.dl_suspended_cb.stateChanged.connect(self._on_changed)
        status_layout.addWidget(self.dl_suspended_cb)
        
        self.dl_submit_scene_cb = QCheckBox("Submit Scene File")
        self.dl_submit_scene_cb.setChecked(True)
        self.dl_submit_scene_cb.setToolTip("Upload and submit the Blender scene (.blend) file as an auxiliary file")
        self.dl_submit_scene_cb.stateChanged.connect(self._on_changed)
        status_layout.addWidget(self.dl_submit_scene_cb)
        
        container_layout.addLayout(status_layout)

        # (Repository Path and Diagnostics are now in Properties dialog)

        root.addWidget(self.container)
        root.addStretch()

        # Set default disabled state
        self._on_enable_toggled()

    def _browse_deadline_cmd(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(self, "Select deadlinecommand executable")
        if chosen:
            self.dl_cmd_edit.setText(chosen)

    def _browse_deadline_repo(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select Deadline Repository Path")
        if chosen:
            self.dl_repo_edit.setText(chosen)

    def _on_enable_toggled(self) -> None:
        enabled = self.use_dl_cb.isChecked()
        self.container.setEnabled(enabled)

    def _select_all_machines(self) -> None:
        self.dl_machines_list.blockSignals(True)
        for i in range(self.dl_machines_list.count()):
            self.dl_machines_list.item(i).setCheckState(Qt.Checked)
        self.dl_machines_list.blockSignals(False)
        self._on_changed()

    def _clear_all_machines(self) -> None:
        self.dl_machines_list.blockSignals(True)
        for i in range(self.dl_machines_list.count()):
            self.dl_machines_list.item(i).setCheckState(Qt.Unchecked)
        self.dl_machines_list.blockSignals(False)
        self._on_changed()

    def get_selected_machines(self) -> str:
        selected = []
        for i in range(self.dl_machines_list.count()):
            item = self.dl_machines_list.item(i)
            if item.checkState() == Qt.Checked:
                selected.append(item.text().strip())
        return ",".join(selected)

    def set_selected_machines(self, whitelist: str) -> None:
        self.dl_machines_list.blockSignals(True)
        allowed = [x.strip() for x in whitelist.split(",") if x.strip() and x.strip().lower() not in ("true", "false")]
        
        # Collect existing items
        existing = {}
        for i in range(self.dl_machines_list.count()):
            item = self.dl_machines_list.item(i)
            existing[item.text().strip()] = item
            
        # If there are allowed items not in the list, add them!
        for name in allowed:
            if name not in existing:
                item = QListWidgetItem(name)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                self.dl_machines_list.addItem(item)
                existing[name] = item
                
        # Set check states
        for name, item in existing.items():
            if name in allowed:
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
                
        self.dl_machines_list.blockSignals(False)


class QueuePanel(QWidget):
    queue_requested = Signal()
    start_selected_requested = Signal()
    start_all_requested = Signal()
    cancel_requested = Signal()
    skip_requested = Signal()
    job_selected = Signal(int)
    job_run_toggled = Signal(int, bool)
    remove_jobs_requested = Signal(object)
    remove_selected_requested = Signal()
    duplicate_jobs_requested = Signal(object)
    job_renamed = Signal(int, str)
    clear_queue_requested = Signal()
    reveal_output_requested = Signal(int)
    open_output_requested = Signal(int)
    move_job_requested = Signal(int, int)  # job_id, delta (-1 up / +1 down)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)
        btns = QHBoxLayout()
        btns.setSpacing(6)
        self.queue_btn = QPushButton("")
        self.queue_btn.setObjectName("IconButton")
        self.queue_btn.setToolTip("New job — add a copy of the current setup to the queue (⌘D duplicates a selected job)")
        self.queue_btn.setFixedSize(36, 30)
        self.queue_btn.clicked.connect(self.queue_requested.emit)
        self.render_selected_btn = QPushButton("")
        self.render_selected_btn.setObjectName("PrimaryButton")
        self.render_selected_btn.setToolTip("Start rendering queued jobs")
        self.render_selected_btn.setFixedSize(40, 30)
        self.render_selected_btn.clicked.connect(self.start_selected_requested.emit)
        self.cancel_btn = QPushButton("")
        self.cancel_btn.setObjectName("DangerButton")
        self.cancel_btn.setToolTip("Stop the current render")
        self.cancel_btn.setFixedSize(40, 30)
        self.cancel_btn.clicked.connect(self.cancel_requested.emit)
        btns.addWidget(self.queue_btn)
        btns.addWidget(self.render_selected_btn)
        btns.addWidget(self.cancel_btn)
        btns.addStretch()
        lay.addLayout(btns)

        # Retained (hidden) so existing render-state wiring keeps working even
        # though Start-All / Skip / Remove buttons were removed from the UI.
        self.render_all_btn = QPushButton(self)
        self.render_all_btn.clicked.connect(self.start_all_requested.emit)
        self.render_all_btn.hide()
        self.skip_btn = QPushButton(self)
        self.skip_btn.clicked.connect(self.skip_requested.emit)
        self.skip_btn.hide()
        self.remove_btn = QPushButton(self)
        self.remove_btn.clicked.connect(self.remove_selected_requested.emit)
        self.remove_btn.hide()
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["Run", "Job", "Preset", "Status", "Progress", "Output"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setColumnWidth(0, 38)
        self.table.setColumnWidth(2, 112)
        self.table.setColumnWidth(3, 62)
        self.table.setColumnWidth(4, 110)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._emit_job_selected)
        self.table.itemChanged.connect(self._on_item_changed)
        # Double-click the Job name to rename it inline.
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table)

        # Delete selected rows with Delete/Backspace; duplicate with Cmd/Ctrl+D.
        del_act = QAction("Delete Selected", self.table)
        del_act.setShortcuts([QKeySequence(Qt.Key_Delete), QKeySequence(Qt.Key_Backspace)])
        del_act.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        del_act.triggered.connect(self.remove_selected_requested.emit)
        self.table.addAction(del_act)
        dup_act = QAction("Duplicate Selected", self.table)
        dup_act.setShortcut(QKeySequence("Ctrl+D"))
        dup_act.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        dup_act.triggered.connect(lambda: self.duplicate_jobs_requested.emit(self._selected_row_job_ids()))
        self.table.addAction(dup_act)

        prog_row = QHBoxLayout()
        prog_row.setContentsMargins(0, 2, 0, 0)
        prog_row.setSpacing(8)
        self.progress_caption = QLabel("Ready")
        self.progress_caption.setObjectName("FieldLabel")
        self.progress_caption.setMinimumWidth(150)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        prog_row.addWidget(self.progress_caption)
        prog_row.addWidget(self.progress_bar, 1)
        lay.addLayout(prog_row)

        self.restyle(active_palette())

    def restyle(self, pal: T.Palette) -> None:
        self.queue_btn.setIcon(icons.icon("plus", pal.text))
        self.render_selected_btn.setIcon(icons.icon("play", pal.accent_text, 15))
        self.cancel_btn.setIcon(icons.icon("stop", pal.danger, 14))

    def set_progress(self, value: float, caption: str) -> None:
        self.progress_bar.setValue(int(max(0, min(100, value))))
        self.progress_caption.setText(caption)

    def set_jobs(self, jobs: list[RenderJob]) -> None:
        from PySide6.QtGui import QColor
        pal = active_palette()
        faint = QColor(pal.text_faint)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for j in jobs:
            r = self.table.rowCount()
            self.table.insertRow(r)
            done = j.status == "success"
            run_item = QTableWidgetItem()
            run_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            run_item.setCheckState(Qt.Checked if j.selected else Qt.Unchecked)
            run_item.setData(Qt.UserRole, j.id)
            if done:
                run_item.setToolTip("Completed — re-check to render again")
            self.table.setItem(r, 0, run_item)
            job_item = QTableWidgetItem(j.label or Path(j.video_path).name or f"Job {j.id}")
            # Only the Job-name cell is editable (double-click to rename); it
            # carries the job id so the rename can be routed back.
            job_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled | Qt.ItemIsEditable)
            job_item.setData(Qt.UserRole, j.id)
            job_item.setToolTip("Double-click to rename")
            self.table.setItem(r, 1, job_item)
            prof_item = QTableWidgetItem(j.output_profile or "H264 MP4")
            prof_item.setFlags(Qt.ItemIsSelectable | Qt.ItemIsEnabled)
            self.table.setItem(r, 2, prof_item)
            status_text = {
                "idle": "idle",
                "running": "run",
                "success": "done",
                "failed": "fail",
                "cancelled": "stop",
            }.get(j.status, j.status)
            status_item = QTableWidgetItem(status_text)
            if j.status == "failed" and j.error:
                status_item.setToolTip(j.error)
            self.table.setItem(r, 3, status_item)
            self.table.setCellWidget(r, 4, self._make_progress_cell(j))
            out_item = QTableWidgetItem(j.output_path)
            if j.output_path:
                out_item.setToolTip(j.output_path)
            self.table.setItem(r, 5, out_item)
            # Completed jobs dim out (Media-Encoder style); re-checking re-activates.
            if done:
                for c in (1, 2, 3, 5):
                    it = self.table.item(r, c)
                    if it is not None:
                        it.setForeground(faint)
        self.table.blockSignals(False)

    def _make_progress_cell(self, job: RenderJob) -> QWidget:
        """A small per-row progress bar, colored by job status."""
        pal = active_palette()
        val = int(max(0, min(100, job.progress)))
        if job.status == "success":
            chunk, label, val = pal.success, "done", 100
        elif job.status == "failed":
            chunk, label, val = pal.danger, "failed", 100
        elif job.status == "cancelled":
            chunk, label = pal.warning, "stopped"
        elif job.status == "running":
            chunk, label = pal.accent, f"{val}%"
        else:
            chunk, label = pal.text_faint, "queued"

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(val if job.status != "idle" else 0)
        bar.setTextVisible(True)
        bar.setFormat(label)
        bar.setAlignment(Qt.AlignCenter)
        bar.setFixedHeight(16)
        bar.setStyleSheet(
            f"QProgressBar{{background:{pal.surface_alt};border:1px solid {pal.border};"
            f"border-radius:5px;text-align:center;color:{pal.text};font-size:10px;}}"
            f"QProgressBar::chunk{{background:{chunk};border-radius:4px;}}"
        )
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(4, 2, 4, 2)
        wl.addWidget(bar)
        return wrap

    def selected_job_ids(self) -> list[int]:
        ids: list[int] = []
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.checkState() == Qt.Checked:
                jid = item.data(Qt.UserRole)
                if isinstance(jid, int):
                    ids.append(jid)
        return ids

    def _emit_job_selected(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            return
        item = self.table.item(r, 0)
        if not item:
            return
        jid = item.data(Qt.UserRole)
        if isinstance(jid, int):
            self.job_selected.emit(jid)

    def select_job(self, job_id: int) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.data(Qt.UserRole) == job_id:
                self.table.setCurrentCell(r, 1)
                break

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        if col == 1:
            item = self.table.item(row, 1)
            if item is not None:
                self.table.editItem(item)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            jid = item.data(Qt.UserRole)
            if isinstance(jid, int):
                self.job_run_toggled.emit(jid, item.checkState() == Qt.Checked)
        elif item.column() == 1:
            jid = item.data(Qt.UserRole)
            name = item.text().strip()
            if isinstance(jid, int) and name:
                self.job_renamed.emit(jid, name)

    def _selected_row_job_ids(self) -> list[int]:
        ids: list[int] = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if not item:
                continue
            jid = item.data(Qt.UserRole)
            if isinstance(jid, int) and jid not in ids:
                ids.append(jid)
        return ids

    def selected_row_job_ids(self) -> list[int]:
        return self._selected_row_job_ids()

    def _show_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row >= 0:
            self.table.selectRow(row)

        selected_ids = self._selected_row_job_ids()
        menu = QMenu(self)
        dup_action = reveal_action = open_action = up_action = down_action = delete_action = None
        if selected_ids:
            first = selected_ids[0]
            dup_action = menu.addAction(f"Duplicate  ({MOD_LABEL}D)")
            menu.addSeparator()
            reveal_action = menu.addAction(f"Reveal Output in {file_manager_name()}")
            open_action = menu.addAction("Open Output")
            menu.addSeparator()
            up_action = menu.addAction("Move Up")
            down_action = menu.addAction("Move Down")
            menu.addSeparator()
            delete_action = menu.addAction("Delete  (⌫)")
        # Clear Queue is always available when there are any jobs.
        clear_action = menu.addAction("Clear Queue…") if self.table.rowCount() else None
        if menu.isEmpty():
            return

        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if action is None:
            return
        if action == dup_action:
            self.duplicate_jobs_requested.emit(selected_ids)
        elif action == delete_action:
            self.remove_jobs_requested.emit(selected_ids)
        elif action == reveal_action:
            self.reveal_output_requested.emit(first)
        elif action == open_action:
            self.open_output_requested.emit(first)
        elif action == up_action:
            self.move_job_requested.emit(first, -1)
        elif action == down_action:
            self.move_job_requested.emit(first, 1)
        elif action == clear_action:
            self.clear_queue_requested.emit()


class PresetBrowserPanel(QWidget):
    load_requested = Signal(object)
    apply_selected_requested = Signal(object)
    apply_checked_requested = Signal(object)
    save_requested = Signal()
    delete_requested = Signal(object)
    refresh_requested = Signal()
    open_folder_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._load_current)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._show_context_menu)
        lay.addWidget(self.list)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        _c = active_palette().text
        self.save_btn = QPushButton("")
        self.save_btn.setToolTip("Save current settings as a preset")
        self.save_btn.setIcon(icons.icon("save", _c))
        self.load_btn = QPushButton("")
        self.load_btn.setToolTip("Load the selected preset")
        self.load_btn.setIcon(icons.icon("download", _c))
        self.apply_checked_btn = QPushButton("")
        self.apply_checked_btn.setToolTip("Apply the selected preset to checked queue jobs")
        self.apply_checked_btn.setIcon(icons.icon("check_apply", _c))
        for b in (self.save_btn, self.load_btn, self.apply_checked_btn):
            b.setObjectName("IconButton")
            b.setFixedSize(36, 28)

        self.save_btn.clicked.connect(self.save_requested.emit)
        self.load_btn.clicked.connect(self._load_current)
        self.apply_checked_btn.clicked.connect(self._apply_checked)

        row1.addWidget(self.save_btn)
        row1.addWidget(self.load_btn)
        row1.addWidget(self.apply_checked_btn)
        row1.addStretch()
        lay.addLayout(row1)

    def set_presets(self, preset_paths: list[Path]) -> None:
        self.list.clear()
        for p in preset_paths:
            item = QListWidgetItem(p.stem)
            item.setData(Qt.UserRole, {"path": str(p), "name": p.stem})
            self.list.addItem(item)

    def _load_current(self, _item: Optional[QListWidgetItem] = None) -> None:
        entry = self._current_entry()
        if entry:
            self.load_requested.emit(entry)

    def _apply_checked(self) -> None:
        entry = self._current_entry()
        if entry:
            self.apply_checked_requested.emit(entry)

    def _apply_selected(self) -> None:
        entry = self._current_entry()
        if entry:
            self.apply_selected_requested.emit(entry)

    def _delete_current(self) -> None:
        entry = self._current_entry()
        if entry and isinstance(entry, dict) and entry.get("path"):
            self.delete_requested.emit(entry)

    def _show_context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is not None:
            self.list.setCurrentItem(item)
        has_current = self.list.currentItem() is not None

        menu = QMenu(self)
        load_action = menu.addAction("Load")
        apply_selected_action = menu.addAction("Apply To Selected Rows")
        apply_checked_action = menu.addAction("Apply To Checked")
        menu.addSeparator()
        delete_action = menu.addAction("Delete Saved Preset")
        menu.addSeparator()
        refresh_action = menu.addAction("Refresh Presets")
        folder_action = menu.addAction("Open Presets Folder")

        for act in (load_action, apply_selected_action, apply_checked_action, delete_action):
            act.setEnabled(has_current)

        chosen = menu.exec(self.list.viewport().mapToGlobal(pos))
        if chosen == load_action:
            self._load_current()
        elif chosen == apply_selected_action:
            self._apply_selected()
        elif chosen == apply_checked_action:
            self._apply_checked()
        elif chosen == delete_action:
            self._delete_current()
        elif chosen == refresh_action:
            self.refresh_requested.emit()
        elif chosen == folder_action:
            self.open_folder_requested.emit()

    def _current_entry(self) -> Optional[object]:
        item = self.list.currentItem()
        if not item:
            return None
        return item.data(Qt.UserRole)


class LogsPanel(QWidget):
    copy_diag = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        # Full history of every line; the view shows everything (detailed),
        # narrowed live by the text filter and the level selector.
        self._raw: list[str] = []
        self._filter_text = ""
        self._level = "All"

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(6)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter logs…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_text)
        self._filter_icon = self.filter_edit.addAction(QIcon(), QLineEdit.LeadingPosition)
        self.level_combo = QComboBox()
        self.level_combo.addItems(["All", "Warnings & errors", "Errors only"])
        self.level_combo.setToolTip("Show only lines at this level")
        self.level_combo.currentTextChanged.connect(self._on_level)
        self.clear_btn = QPushButton("")
        self.clear_btn.setObjectName("IconButton")
        self.clear_btn.setToolTip("Clear the log")
        self.clear_btn.setFixedSize(30, 24)
        self.clear_btn.clicked.connect(self._clear)
        self.copy_btn = QPushButton("")
        self.copy_btn.setObjectName("IconButton")
        self.copy_btn.setToolTip("Copy diagnostics to clipboard")
        self.copy_btn.setFixedSize(30, 24)
        self.copy_btn.clicked.connect(self.copy_diag.emit)
        hdr.addWidget(self.filter_edit, 1)
        hdr.addWidget(self.level_combo)
        hdr.addWidget(self.clear_btn)
        hdr.addWidget(self.copy_btn)
        lay.addLayout(hdr)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        _mono = QFont()
        _mono.setFamilies(["Menlo", "Consolas", "DejaVu Sans Mono", "Courier New"])
        _mono.setStyleHint(QFont.Monospace)   # guaranteed monospace fallback on any OS
        _mono.setPointSize(10)
        self.text.setFont(_mono)
        lay.addWidget(self.text)

        self.restyle(active_palette())

    def restyle(self, pal: T.Palette) -> None:
        self.clear_btn.setIcon(icons.icon("trash", pal.text, 14))
        self.copy_btn.setIcon(icons.icon("copy", pal.text, 14))
        self._filter_icon.setIcon(icons.icon("search", pal.text_muted, 14))

    # ── filtering ─────────────────────────────────────────────────────────
    def _on_filter_text(self, text: str) -> None:
        self._filter_text = text.strip().lower()
        self._rerender()

    def _on_level(self, level: str) -> None:
        self._level = level
        self._rerender()

    @staticmethod
    def _is_error(low: str) -> bool:
        return any(k in low for k in ("error", "traceback", "failed", "cannot", "exception", "critical", "not found"))

    @staticmethod
    def _is_warning(low: str) -> bool:
        return any(k in low for k in ("warning", "warn", "skipped", "timeout", "unavailable"))

    def _passes(self, line: str) -> bool:
        low = line.lower()
        if self._filter_text and self._filter_text not in low:
            return False
        if self._level == "Errors only":
            return self._is_error(low)
        if self._level == "Warnings & errors":
            return self._is_error(low) or self._is_warning(low)
        return True

    def _rerender(self) -> None:
        self.text.clear()
        for line in self._raw:
            self._emit(line)

    def _emit(self, line: str) -> None:
        import html
        if not self._passes(line):
            return
        color = self._line_color(line, active_palette())
        safe = html.escape(line).replace(" ", "&nbsp;")
        self.text.append(f'<span style="color:{color}; white-space:pre;">{safe}</span>')

    @staticmethod
    def _line_color(line: str, pal: T.Palette) -> str:
        low = line.lower()
        if any(k in low for k in ("error", "traceback", "failed", "not found", "cannot", "exception", "critical")):
            return pal.danger
        if any(k in low for k in ("warning", "warn", "skipped", "timeout", "unavailable")):
            return pal.warning
        if any(k in low for k in ("finished successfully", "render finished", "complete", "completed",
                                  "success", "installed", "connected", "enabled", "saved", "done")):
            return pal.success
        if "[app]" in line:   # tolerant of a leading [HH:MM:SS] timestamp
            return pal.info
        if "fra:" in low:
            return pal.text_faint
        return pal.text_muted

    def append(self, line: str) -> None:
        self._raw.append(line)
        if len(self._raw) > 8000:          # cap memory for very long sessions
            self._raw = self._raw[-6000:]
            self._rerender()
            return
        self._emit(line)

    def _clear(self) -> None:
        self._raw.clear()
        self.text.clear()


class _PreviewImage(QLabel):
    """Paints its image scaled-to-fit (Fit) or at 1:1 pixels (100%). Double-click
    toggles between the two; at 100% it can be grabbed and panned. Falls back to
    placeholder text when there is no image."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._src: Optional[QPixmap] = None
        self._scroll: Optional[QScrollArea] = None   # set by the panel
        self._on_toggle = None                       # callback(QPointF) on dbl-click
        self._pannable = False
        self._panning = False
        self._pan_anchor = None
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

    def set_source(self, pm: QPixmap) -> None:
        self._src = pm
        self.update()

    def clear_source(self) -> None:
        self._src = None
        self.update()

    def set_fit(self) -> None:
        """Fill the scroll viewport and scale-to-fit."""
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        self.update()

    def set_fixed(self, w: int, h: int) -> None:
        """Pin to an exact pixel size (1:1); the scroll area pans when this is
        larger than the viewport."""
        self.setFixedSize(max(1, int(w)), max(1, int(h)))
        self.update()

    def set_pannable(self, on: bool) -> None:
        self._pannable = on
        if not self._panning:
            self.setCursor(Qt.OpenHandCursor if on else Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if self._src is not None and not self._src.isNull() and self._on_toggle:
            self._on_toggle(event.position())

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if self._pannable and event.button() == Qt.LeftButton and self._scroll is not None:
            self._panning = True
            self._pan_anchor = event.globalPosition().toPoint()
            self._h0 = self._scroll.horizontalScrollBar().value()
            self._v0 = self._scroll.verticalScrollBar().value()
            self.setCursor(Qt.ClosedHandCursor)
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._panning and self._scroll is not None:
            delta = event.globalPosition().toPoint() - self._pan_anchor
            self._scroll.horizontalScrollBar().setValue(self._h0 - delta.x())
            self._scroll.verticalScrollBar().setValue(self._v0 - delta.y())
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._panning:
            self._panning = False
            self.setCursor(Qt.OpenHandCursor if self._pannable else Qt.ArrowCursor)
        else:
            super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if self._src is None or self._src.isNull():
            super().paintEvent(event)   # placeholder text
            return
        scaled = self._src.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawPixmap(x, y, scaled)
        painter.end()


class PreviewPanel(QWidget):
    """Shows live rendered frames during a render, then plays the finished
    output video itself (looped) when the render completes."""

    preview_frame_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(6)
        self.caption = QLabel("Live frame preview")
        self.caption.setObjectName("FieldLabel")
        head.addWidget(self.caption)
        head.addStretch()
        # Preview render scale — full resolution by default, with quick fractions
        # for faster (lower-res) previews.
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["Full", "1/2", "1/4", "1/8"])
        self.scale_combo.setToolTip("Preview render resolution (fraction of the output resolution)")
        self.scale_combo.setMinimumWidth(78)
        self.scale_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        head.addWidget(self.scale_combo)
        self.auto_btn = QPushButton()
        self.auto_btn.setObjectName("IconButton")
        self.auto_btn.setFixedSize(34, 28)
        self.auto_btn.setCheckable(True)
        self.auto_btn.setCursor(Qt.PointingHandCursor)
        self.auto_btn.setToolTip("Auto-render: re-render the preview whenever you change a setting or scrub")
        self.auto_btn.toggled.connect(self._on_auto_toggled)
        head.addWidget(self.auto_btn)
        self.preview_frame_btn = QPushButton()
        self.preview_frame_btn.setObjectName("IconButton")
        self.preview_frame_btn.setFixedSize(34, 28)
        self.preview_frame_btn.setCursor(Qt.PointingHandCursor)
        self.preview_frame_btn.setToolTip("Render the selected frame with the current mappings")
        self.preview_frame_btn.clicked.connect(self.preview_frame_requested.emit)
        head.addWidget(self.preview_frame_btn)
        lay.addLayout(head)

        # ── frame scrubber — a self-contained bar to choose the frame ────────
        self._sync_guard = False

        def _step_btn(tip: str, delta: int) -> QPushButton:
            b = QPushButton()
            b.setObjectName("SmallButton")
            b.setFixedSize(28, 26)
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip(tip)
            b.setAutoRepeat(True)
            b.clicked.connect(lambda: self._nudge_frame(delta))
            return b

        scrub = QHBoxLayout()
        scrub.setContentsMargins(10, 7, 12, 7)
        scrub.setSpacing(8)
        self.frame_icon = QLabel()
        self.frame_icon.setFixedSize(16, 16)
        self.prev_btn = _step_btn("Previous frame", -1)
        self.next_btn = _step_btn("Next frame", +1)
        self.frame_slider = QSlider(Qt.Horizontal)
        self.frame_slider.setMinimum(1)
        self.frame_slider.setMaximum(1)
        self.frame_slider.setToolTip("Drag to pick the frame to preview")
        self.frame_spin = QSpinBox()
        self.frame_spin.setObjectName("FrameSpin")
        self.frame_spin.setMinimum(1)
        self.frame_spin.setMaximum(1)
        self.frame_spin.setFixedWidth(66)
        self.frame_spin.setAlignment(Qt.AlignCenter)
        self.frame_spin.setButtonSymbols(QSpinBox.NoButtons)
        self.total_lbl = QLabel("/ 1")
        self.total_lbl.setObjectName("HintLabel")

        self.frame_slider.valueChanged.connect(self._on_slider)
        self.frame_spin.valueChanged.connect(self._on_spin)
        # Auto-render fires on a *settled* value (slider released / spin commit)
        # rather than every intermediate tick, so dragging spawns one render.
        self.scale_combo.currentTextChanged.connect(lambda _v: self._maybe_auto_render())
        self.frame_slider.sliderReleased.connect(self._maybe_auto_render)
        self.frame_spin.editingFinished.connect(self._maybe_auto_render)

        scrub.addWidget(self.frame_icon)
        scrub.addWidget(self.prev_btn)
        scrub.addWidget(self.frame_slider, 1)
        scrub.addWidget(self.next_btn)
        scrub.addWidget(self.frame_spin)
        scrub.addWidget(self.total_lbl)
        self.frame_row_widget = QFrame()
        self.frame_row_widget.setObjectName("ScrubBar")
        self.frame_row_widget.setLayout(scrub)
        lay.addWidget(self.frame_row_widget)

        self._pixmap: Optional[QPixmap] = None
        self.image_label = _PreviewImage("No preview yet.\nStart a render with Live Preview enabled.")
        self.image_label.setObjectName("HintLabel")
        self.image_label.setToolTip("Double-click to toggle Fit ⇄ 100% · drag to pan at 100%")
        # Scroll area lets fixed-zoom previews (100%, 200%…) pan; in Fit mode the
        # label is resized to the viewport and scaled to fit.
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.image_label)
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.image_label._scroll = self.scroll
        self.image_label._on_toggle = self._toggle_zoom
        self._zoom_100 = False

        self._has_video = _HAS_MULTIMEDIA
        if self._has_video:
            self.stack = QStackedWidget()
            frame_wrap = QWidget()
            fl = QVBoxLayout(frame_wrap)
            fl.setContentsMargins(0, 0, 0, 0)
            fl.addWidget(self.scroll)
            self.stack.addWidget(frame_wrap)            # index 0 — live frames
            self.video_widget = QVideoWidget()
            self.stack.addWidget(self.video_widget)     # index 1 — finished video
            lay.addWidget(self.stack, 1)

            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.audio.setMuted(True)
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video_widget)
            try:
                self.player.setLoops(QMediaPlayer.Infinite)
            except Exception:
                self.player.mediaStatusChanged.connect(self._loop_if_ended)

            ctrl = QHBoxLayout()
            ctrl.setContentsMargins(0, 0, 0, 0)
            self.play_btn = QPushButton("Pause")
            self.play_btn.setObjectName("SmallButton")
            self.play_btn.clicked.connect(self._toggle_play)
            self.mute_btn = QPushButton("Unmute")
            self.mute_btn.setObjectName("SmallButton")
            self.mute_btn.clicked.connect(self._toggle_mute)
            ctrl.addWidget(self.play_btn)
            ctrl.addWidget(self.mute_btn)
            ctrl.addStretch()
            self.controls = QWidget()
            self.controls.setLayout(ctrl)
            self.controls.setVisible(False)
            lay.addWidget(self.controls)
        else:
            lay.addWidget(self.scroll, 1)

        self.auto_btn.setChecked(True)   # auto-render on by default
        self.restyle(active_palette())

    # ── frame picker ─────────────────────────────────────────────────────
    def restyle(self, palette) -> None:
        """Re-tint the scrubber icons for the active palette (called on theme
        change, and once at construction)."""
        self.frame_icon.setPixmap(icons.pixmap("film", palette.text_muted, 16))
        self.prev_btn.setIcon(icons.icon("chevron_left", palette.text, 16))
        self.next_btn.setIcon(icons.icon("chevron_right", palette.text, 16))
        self.preview_frame_btn.setIcon(icons.icon("camera", palette.text, 16))
        self._retint_auto_icon()

    def _retint_auto_icon(self) -> None:
        pal = active_palette()
        col = pal.accent_text if self.auto_btn.isChecked() else pal.text
        self.auto_btn.setIcon(icons.icon("refresh", col, 15))

    def _on_auto_toggled(self, on: bool) -> None:
        self._retint_auto_icon()
        # Turning Auto on renders the current frame immediately for feedback.
        if on and self.frame_row_widget.isEnabled():
            self.preview_frame_requested.emit()

    def _maybe_auto_render(self) -> None:
        if self.auto_btn.isChecked() and self.frame_row_widget.isEnabled():
            self.preview_frame_requested.emit()

    def _nudge_frame(self, delta: int) -> None:
        before = self.frame_spin.value()
        self.frame_spin.setValue(before + delta)
        if self.frame_spin.value() != before:
            self._maybe_auto_render()

    def _on_slider(self, value: int) -> None:
        if self._sync_guard:
            return
        self._sync_guard = True
        self.frame_spin.setValue(value)
        self._sync_guard = False

    def _on_spin(self, value: int) -> None:
        if self._sync_guard:
            return
        self._sync_guard = True
        self.frame_slider.setValue(value)
        self._sync_guard = False

    def set_frame_range(self, start: int, end: int) -> None:
        """Point the slider/spin at the render's frame range, keeping the
        current selection where possible. A single-frame range disables the
        picker (nothing to scrub)."""
        lo, hi = (start, end) if start <= end else (end, start)
        cur = self.current_frame()
        self._sync_guard = True
        for w in (self.frame_slider, self.frame_spin):
            w.setMinimum(lo)
            w.setMaximum(hi)
            w.setValue(min(max(cur, lo), hi))
        self.frame_slider.setPageStep(max(1, (hi - lo) // 10))
        self._sync_guard = False
        self.total_lbl.setText(f"/ {hi}")
        self.frame_row_widget.setEnabled(hi > lo)

    def current_frame(self) -> int:
        return self.frame_spin.value()

    def preview_scale(self) -> float:
        return {"Full": 1.0, "1/2": 0.5, "1/4": 0.25, "1/8": 0.125}.get(self.scale_combo.currentText(), 1.0)

    def _goto_fit(self) -> None:
        self._zoom_100 = False
        self.scroll.setWidgetResizable(True)
        self.image_label.set_pannable(False)
        self.image_label.set_fit()

    def _goto_100(self, focus=None) -> None:
        """Switch to 1:1 pixels. ``focus`` (a QPointF in the label's current
        coordinates) is the point to keep centred — so double-clicking a spot
        zooms in on that spot, not the middle."""
        if self._pixmap is None or self._pixmap.isNull():
            return
        self._zoom_100 = True
        iw, ih = self._pixmap.width(), self._pixmap.height()
        # Map the click (in the Fit-scaled, letterboxed label) to a source pixel.
        cx, cy = iw / 2.0, ih / 2.0
        if focus is not None:
            lw, lh = self.image_label.width(), self.image_label.height()
            fit = min(lw / iw, lh / ih) if iw and ih else 1.0
            off_x, off_y = (lw - iw * fit) / 2.0, (lh - ih * fit) / 2.0
            cx = min(max((focus.x() - off_x) / fit, 0.0), iw)
            cy = min(max((focus.y() - off_y) / fit, 0.0), ih)
        self.scroll.setWidgetResizable(False)
        self.image_label.set_fixed(iw, ih)
        self.image_label.set_pannable(True)

        def _center():
            vp = self.scroll.viewport()
            self.scroll.horizontalScrollBar().setValue(int(cx - vp.width() / 2))
            self.scroll.verticalScrollBar().setValue(int(cy - vp.height() / 2))
        QTimer.singleShot(0, _center)   # after the resize/layout settles

    def _toggle_zoom(self, focus=None) -> None:
        if self._zoom_100:
            self._goto_fit()
        else:
            self._goto_100(focus)

    # ── live frames ──────────────────────────────────────────────────────
    def set_image_path(self, path: str) -> None:
        if self._has_video and self.stack.currentIndex() != 0:
            self._stop_video()
            self.stack.setCurrentIndex(0)
            self.controls.setVisible(False)
        pm = QPixmap(path)
        if pm.isNull():
            return
        self._pixmap = pm
        self.caption.setText("Live frame preview")
        self.image_label.set_source(pm)   # _PreviewImage rescales itself on paint/resize
        # Keep the current zoom across new frames; at 100% re-pin to the new
        # frame's pixel size (same resolution → preserves the pan position).
        if self._zoom_100:
            self.image_label.set_fixed(pm.width(), pm.height())

    def clear_preview(self) -> None:
        self._pixmap = None
        if self._has_video:
            self._stop_video()
            self.stack.setCurrentIndex(0)
            self.controls.setVisible(False)
        self.image_label.clear_source()
        self.image_label.setText("No preview yet.\nStart a render with Live Preview enabled.")
        self.caption.setText("Live frame preview")
        self._goto_fit()

    # ── finished video playback ──────────────────────────────────────────
    def play_video(self, path: str) -> None:
        if not self._has_video or not path:
            return
        from PySide6.QtCore import QUrl as _QUrl
        self.caption.setText(f"Rendered video · {Path(path).name}")
        self.player.setSource(_QUrl.fromLocalFile(path))
        self.stack.setCurrentIndex(1)
        self.controls.setVisible(True)
        self.play_btn.setText("Pause")
        self.player.play()

    def _stop_video(self) -> None:
        try:
            self.player.stop()
        except Exception:
            pass

    def _loop_if_ended(self, status) -> None:
        try:
            if status == QMediaPlayer.EndOfMedia:
                self.player.setPosition(0)
                self.player.play()
        except Exception:
            pass

    def _toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.play_btn.setText("Play")
        else:
            self.player.play()
            self.play_btn.setText("Pause")

    def _toggle_mute(self) -> None:
        muted = not self.audio.isMuted()
        self.audio.setMuted(muted)
        self.mute_btn.setText("Unmute" if muted else "Mute")


class BlenderVideoMapperQt(QMainWindow):
    _update_checked = Signal(object, bool)   # (manifest dict | None, was-manual)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(_make_app_icon())
        self.resize(1400, 860)

        self._blender_path = ""
        self._c4dpy_path = _find_c4dpy()   # Cinema 4D headless Python, if installed
        self._deadline_repo_path = ""
        self._deadline_command_path = ""
        self._deadline_job_name_template = "BlenderRender Job - {scene_name}"
        self._deadline_comment = ""
        self._discovered_materials: list[str] = []
        self._discovered_cameras: list[str] = []
        self._jobs: list[RenderJob] = []
        self._next_job_id = 1
        self._is_rendering = False
        self._scan_in_progress = False
        self._known_videos: set[str] = set()
        self._ffmpeg_hint_shown = False
        self._active_job_id: Optional[int] = None
        self._loading_job_into_ui = False

        self._render_thread: Optional[RenderThread] = None
        self._discovery_thread: Optional[DiscoveryThread] = None
        self._runtime_install_thread: Optional[RuntimeInstallThread] = None
        self._runtime_prompted = False
        self._save_timer: Optional[QTimer] = None

        self._theme_mode = "dark"
        self._accent = T.ACCENT_ORANGE
        self._palette: T.Palette = T.build_palette(self._theme_mode, self._accent)
        self._toast: Optional[QWidget] = None
        self._toast_anim: Optional[QPropertyAnimation] = None

        self._preview_enabled = True
        self._preview_path = ""
        self._preview_timer: Optional[QTimer] = None
        self._job_started: dict[int, float] = {}
        self._render_t0 = 0.0
        self._custom_layout_state = ""
        self._current_layout = "default"
        self._restored_geometry = False
        self._recent_scenes: list[str] = []
        self._when_done = "nothing"           # nothing | quit | sleep
        # Auto-render (target-driven): when all marked target materials have a
        # clip, queue one multi-screen render; re-fire on new versions.
        self._autorender_enabled = False
        self._autorender_output = ""          # where PREVIZ renders go (≠ watch folder)
        self._autorender_pattern = "{clip}_PREVIZ"
        self._autorender_start = False        # auto-start vs queue-only
        self._last_report_path = ""
        self._job_durations: dict[int, float] = {}
        self._preview_thread: Optional[PreviewFrameThread] = None
        self._update_checked.connect(self._on_update_checked)
        self._undo_stack: list = []      # (description, restore_callable) for destructive actions

        self._apply_theme()
        self._build_menu()
        self._build_layout()
        self._build_status_bar()
        self._load_profile()
        self._update_health()
        self._update_status_bar()
        # Quietly check the update share a few seconds after launch.
        QTimer.singleShot(3000, lambda: self._check_for_updates(manual=False))
        QTimer.singleShot(300, self._maybe_first_run)   # one-time welcome on first launch
        # Size/position the window once it's shown: restore the user's last
        # adjustment if it's reasonable, otherwise default to 70% of the screen
        # centered.
        QTimer.singleShot(0, self._init_window_geometry)
        QTimer.singleShot(250, self._init_blender)

    def _apply_theme(self) -> None:
        self._palette = T.build_palette(self._theme_mode, self._accent)
        set_active_palette(self._palette)
        self.setStyleSheet(T.stylesheet(self._palette))

    def _restyle_all(self) -> None:
        """Rebuild stylesheet and re-tint every icon for the active palette."""
        self._apply_theme()
        icons.clear_cache()
        for panel in (
            getattr(self, "scene_panel", None),
            getattr(self, "render_panel", None),
            getattr(self, "deadline_panel", None),
            getattr(self, "queue_panel", None),
            getattr(self, "logs_panel", None),
            getattr(self, "preview_panel", None),
        ):
            if panel is not None and hasattr(panel, "restyle"):
                panel.restyle(self._palette)
        self.setWindowIcon(_make_app_icon())
        if hasattr(self, "_flow_steps"):
            self._update_health()
        # Rebuild the queue so per-row progress bars pick up the new palette.
        if getattr(self, "_jobs", None) is not None and hasattr(self, "queue_panel"):
            self._refresh_queue_view()

    def _build_menu(self) -> None:
        mb = self.menuBar()

        edit = mb.addMenu("Edit")
        self._undo_action = QAction("Undo", self)
        self._undo_action.setShortcut(QKeySequence.Undo)   # ⌘Z / Ctrl+Z
        self._undo_action.setEnabled(False)
        self._undo_action.triggered.connect(self._undo)
        edit.addAction(self._undo_action)

        profile = mb.addMenu("Profile")
        profile.addAction("Properties…", self._show_properties_dialog)
        profile.addSeparator()
        profile.addAction("Open Project…", self._open_project)
        profile.addAction("Save Project As…", self._save_project)
        profile.addSeparator()
        profile.addAction("Save Preset…", self._save_preset)
        profile.addAction("Load Preset…", self._load_preset)
        profile.addAction("Open Presets Folder", self._open_presets_folder)

        tools = mb.addMenu("Tools")
        tools.addAction("Export Prepared .blend for Render Farm…", self._export_prepared_blend)
        tools.addSeparator()
        tools.addAction("Render History…", self._show_history_dialog)
        self._open_report_action = QAction("Open Last Run Report", self)
        self._open_report_action.triggered.connect(self._open_last_report)
        self._open_report_action.setEnabled(False)
        tools.addAction(self._open_report_action)
        tools.addSeparator()
        tools.addAction("Copy Diagnostics", self._copy_diagnostics)
        tools.addSeparator()
        when_menu = tools.addMenu("When Render Finishes")
        when_group = QActionGroup(self)
        when_group.setExclusive(True)
        for label, val in (("Do Nothing", "nothing"), ("Quit App", "quit"), ("Sleep Computer", "sleep")):
            act = QAction(label, self, checkable=True)
            act.setChecked(val == self._when_done)
            act.triggered.connect(lambda _c=False, v=val: setattr(self, "_when_done", v) or self._schedule_save())
            when_group.addAction(act)
            when_menu.addAction(act)
        self._when_actions = {a.text(): a for a in when_group.actions()}

        runtime = mb.addMenu("Runtime")
        runtime.addAction("Install Managed Blender", self._install_managed_runtime)
        runtime.addAction("Locate Blender…", self._show_properties_dialog)

        deadline = mb.addMenu("Deadline")
        deadline.addAction("Test Connection", self._test_deadline_connection)
        deadline.addAction("Export Current Job Files…", self._export_deadline_files)

        self.view_menu = mb.addMenu("View")
        layout_menu = self.view_menu.addMenu("Layout")
        layout_menu.addAction("Default (3 columns)", lambda: self._apply_named_layout("default"))
        layout_menu.addAction("All Panels (grid)", lambda: self._apply_named_layout("grid"))
        layout_menu.addAction("Render Focus", lambda: self._apply_named_layout("focus"))
        layout_menu.addAction("Setup Focus", lambda: self._apply_named_layout("setup"))
        layout_menu.addAction("Stacked (compact)", lambda: self._apply_named_layout("stacked"))
        layout_menu.addAction("Tabbed (single pane)", lambda: self._apply_named_layout("tabbed"))
        layout_menu.addSeparator()
        layout_menu.addAction("Save Current Layout", self._save_custom_layout)
        self.restore_layout_action = QAction("My Saved Layout", self)
        self.restore_layout_action.triggered.connect(self._restore_custom_layout)
        self.restore_layout_action.setEnabled(bool(self._custom_layout_state))
        layout_menu.addAction(self.restore_layout_action)

        self.view_menu.addSeparator()
        self.preview_action = QAction("Live Preview While Rendering", self, checkable=True)
        self.preview_action.setChecked(self._preview_enabled)
        self.preview_action.toggled.connect(self._set_preview_enabled)
        self.view_menu.addAction(self.preview_action)

        help_menu = mb.addMenu("Help")
        help_menu.addAction("Quick Start", self._show_quick_start)
        help_menu.addAction("Keyboard Shortcuts", self._show_shortcuts_help)
        help_menu.addAction("Check for Updates…", lambda: self._check_for_updates(manual=True))
        help_menu.addSeparator()
        help_menu.addAction(f"About {APP_NAME}", self._show_about)

        self._build_shortcuts()

    def _build_shortcuts(self) -> None:
        def add(seq: str, slot) -> None:
            act = QAction(self)
            act.setShortcut(QKeySequence(seq))
            act.triggered.connect(slot)
            self.addAction(act)

        add("Ctrl+R", lambda: self._start_render(render_all=False))
        add("Ctrl+Shift+R", lambda: self._start_render(render_all=True))
        add("Ctrl+.", self._cancel_render)
        add("Ctrl+B", lambda: self.scene_panel._browse_scene())

    def _set_preview_enabled(self, on: bool) -> None:
        self._preview_enabled = on
        self._schedule_save()

    # ── Help ──────────────────────────────────────────────────────────────
    def _show_help_dialog(self, title: str, html: str) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(560, 460)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        pal = self._palette
        browser.setStyleSheet(f"QTextBrowser {{ border: none; background: {pal.surface}; padding: 16px; }}")
        browser.setHtml(html)
        lay.addWidget(browser)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.exec()

    def _help_css(self) -> str:
        p = self._palette
        return (
            f"<style>"
            f"body{{color:{p.text}; font-size:13px; line-height:1.5;}}"
            f"h2{{color:{p.accent}; font-size:15px; margin:14px 0 6px;}}"
            f"h3{{color:{p.text}; font-size:13px; margin:12px 0 4px;}}"
            f"code,kbd{{background:{p.surface_alt}; color:{p.text}; padding:1px 6px; border-radius:4px; font-family:Menlo,monospace;}}"
            f"li{{margin:3px 0;}} a{{color:{p.info};}}"
            f"table{{border-collapse:collapse; margin:4px 0;}}"
            f"td{{padding:3px 14px 3px 0; vertical-align:top;}}"
            f".muted{{color:{p.text_muted};}}"
            f"</style>"
        )

    def _show_quick_start(self) -> None:
        html = self._help_css() + """
        <h2>Quick Start</h2>
        <ol>
          <li><b>Pick a scene</b> — drop a 3D file onto the Scene field (or click <i>Browse</i>),
              then click <b>Scan Scene</b> to load its materials and cameras. Blender (<code>.blend</code>,
              <code>.fbx</code>, <code>.usd</code>, …) and <b>Cinema&nbsp;4D</b> (<code>.c4d</code>) scenes
              are both supported — see below.</li>
          <li><b>Add videos</b> — click <i>Add</i> or drag &amp; drop clips into the Videos list.
              Pick a <b>Camera</b> for the render.</li>
          <li><b>Connect</b> — select a material and a video, then click the <b>link</b> button
              between the lists. A colored <b>stripe</b> marks the connected pair; hover or select
              either side to light up its partner. Or just use <b>Auto-map</b> (below).</li>
          <li><b>Output</b> auto-fills next to the source clip — edit it if you like. Set resolution,
              frame range, format, and (optionally) the collapsed <i>Advanced quality settings</i>.</li>
          <li>Click <b>Queue</b> to add the job, then <b>Start</b> (or submit to a <b>render farm</b>).
              Watch per-row progress and a live frame preview.</li>
        </ol>
        <h3>Auto-map by name &amp; Watch folder</h3>
        <p class="muted">A clip is linked to a material automatically when the material's name
        appears in the clip's filename (e.g. <code>Screen</code> ↔ <code>Screen_final_v3.mp4</code>).
        This runs the moment you <b>import</b> a clip, or on demand with the <b>Auto-map</b> button —
        it only fills empty materials, never overwriting a manual mapping.</p>
        <p class="muted">Turn on the <b>Watch folder</b> (clock button under the clip list) and any
        clip dropped into that folder is imported and mapped automatically. <b>Versions</b> are
        understood — <code>Screen_v1</code>/<code>Screen_v2</code>/<code>Screen_3</code> are one clip
        and the <b>latest wins</b>; if a newer version appears, the project updates to it
        automatically. Files still being copied are skipped until complete. Tune the poll interval
        and stability window in <i>Properties → General → Watch / Ingest</i>.</p>
        <h3>Auto-render targets</h3>
        <p class="muted">Right-click a material → <b>Mark as Render Target</b> to flag the screens
        a finished render must cover. With <b>Auto-render</b> on (<i>Properties → General →
        Auto-render</i>), the watch folder queues <b>one multi-screen render</b> the moment every
        target has a clip — and queues a fresh one whenever a newer version of a target lands. The
        output is named after the clips with a <code>PREVIZ</code> suffix (customizable), and lands
        in its own folder so it's never re-ingested. Choose <i>queue only</i> or <i>start
        automatically</i>.</p>
        <h3>Cinema 4D + Redshift</h3>
        <p class="muted">Import a <code>.c4d</code> scene and the renderer switches to <b>Redshift</b>
        automatically. The clip is mapped to a material's Redshift emission (full-bright). The
        Advanced panel shows Redshift controls — a <b>Speed Preset</b> (Draft→Final), Max/Min samples,
        adaptive <b>Noise Threshold</b>, Denoise, GI bounces / on-off, and Max Ray Depth — to trade
        quality for render time. Settings panels adapt to the active renderer so every control is real.</p>
        <h3>Render farm (Deadline)</h3>
        <p class="muted">Enable the <b>Deadline</b> panel to submit jobs to a Thinkbox Deadline farm.
        Blender jobs run via the worker on each node; <b>Cinema 4D</b> jobs are baked into a
        self-contained scene and rendered with the licensed Cinema 4D command-line renderer (the same
        engine the stock Cinema4D plugin uses), so node licensing just works. Frames distribute across
        nodes, and jobs show the app icon in the Deadline Monitor.</p>
        <h3>Audio</h3>
        <p class="muted">Any clip that contains sound shows a <b>speaker</b> badge in the Videos
        list. Click the badge (or right-click → <i>Mute audio</i>) to drop that clip's audio; every
        non-muted mapped clip is mixed into the rendered video.</p>
        <h3>Queue</h3>
        <p class="muted">Click a row to activate and edit it; <b>double-click the name</b> to rename.
        New jobs (the <b>+</b> button) are added at the top. Duplicate with <kbd>⌘D</kbd>,
        delete with <kbd>⌫</kbd>, or right-click for Duplicate / Reveal / Open / Move / Delete.
        Tick the <b>Run</b> box to include a job when you press Start.</p>
        <h3>Layout &amp; appearance</h3>
        <p class="muted"><i>View → Layout</i> offers Default, All Panels (grid), Render Focus,
        Setup Focus, Stacked and Tabbed presets, plus <b>Save Current Layout</b>. Drag a panel's
        tab to rearrange, tab, or float it, and show/hide panels from <i>View</i>.
        <i>View → Theme / Accent Color</i> restyle the app. The window opens at 70% of your
        screen, centered, and remembers your size and position.</p>
        """
        self._show_help_dialog("Quick Start", html)

    def _show_shortcuts_help(self) -> None:
        rows = [
            ("⌘R", "Start render (queued jobs ticked Run)"),
            ("⌘⇧R", "Start all queued jobs"),
            ("⌘.", "Stop the current render"),
            ("⌘B", "Browse for a scene file"),
            ("⌘D", "Duplicate selected queue job(s)"),
            ("⌫ / Delete", "Delete selected queue job(s)"),
        ]
        body = "".join(f"<tr><td><kbd>{k}</kbd></td><td>{d}</td></tr>" for k, d in rows)
        html = self._help_css() + f"""
        <h2>Keyboard Shortcuts</h2>
        <table>{body}</table>
        <p class="muted">On Windows/Linux, ⌘ is Ctrl. Queue shortcuts apply when the Queue has focus.</p>
        """
        self._show_help_dialog("Keyboard Shortcuts", html)

    @staticmethod
    def _logo_path() -> Optional[Path]:
        roots = [Path(__file__).resolve().parent]
        if getattr(sys, "frozen", False):
            roots.insert(0, Path(getattr(sys, "_MEIPASS", "")))
        for root in roots:
            for name in ("toy_robot_media.png", "toy_robot_media_logo.png", "toy_robot_media.svg"):
                p = root / "assets" / name
                if p.exists():
                    return p
        return None

    def _show_about(self) -> None:
        pal = self._palette
        dlg = QDialog(self)
        dlg.setWindowTitle(f"About {APP_NAME}")
        dlg.setFixedWidth(420)
        dlg.setStyleSheet(f"QDialog {{ background: {pal.window}; }}")
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(28, 26, 28, 22)
        lay.setSpacing(6)
        lay.setAlignment(Qt.AlignHCenter)

        def centered(w):
            w.setAlignment(Qt.AlignCenter)
            lay.addWidget(w, 0, Qt.AlignHCenter)
            return w

        lay.addWidget(_ImageView(_make_app_icon().pixmap(QSize(92, 92)), pal.window),
                      0, Qt.AlignHCenter)

        name = centered(QLabel(APP_NAME))
        name.setStyleSheet(f"color:{pal.text}; font-size:19px; font-weight:700; margin-top:8px;")
        ver = centered(QLabel(f"Version {APP_VERSION}"))
        ver.setStyleSheet(f"color:{pal.text_muted}; font-size:12px;")
        desc = centered(QLabel("Automated video-texture mapping and\nheadless rendering for Blender."))
        desc.setStyleSheet(f"color:{pal.text_muted}; font-size:12px; margin-top:8px;")
        desc.setWordWrap(True)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color:{pal.border}; margin:14px 40px;")
        lay.addWidget(sep)

        powered = centered(QLabel("Powered by"))
        powered.setStyleSheet(f"color:{pal.text_faint}; font-size:11px;")
        logo = self._logo_path()
        if logo is not None:
            pm = QPixmap(str(logo))
            if not pm.isNull():
                scaled = pm.scaledToWidth(260, Qt.SmoothTransformation)  # 2× of 130 for Retina
                scaled.setDevicePixelRatio(2.0)
                lay.addWidget(_ImageView(scaled, pal.window), 0, Qt.AlignHCenter)
            else:
                logo = None
        if logo is None:
            brand = centered(QLabel("Toy Robot Media"))
            brand.setStyleSheet(f"color:{pal.text}; font-size:14px; font-weight:700;")

        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addSpacing(10)
        lay.addWidget(btns)
        dlg.exec()

    def _build_layout(self) -> None:
        self.setDockOptions(
            QMainWindow.AllowNestedDocks
            | QMainWindow.AllowTabbedDocks
            | QMainWindow.AnimatedDocks
            | QMainWindow.GroupedDragging
        )
        # Tabs on top (the tab acts as the panel header) instead of Qt's default
        # bottom placement.
        self.setTabPosition(Qt.AllDockWidgetAreas, QTabWidget.North)

        # No central widget at all: the docks fill the whole window. A zero-size
        # central widget would otherwise leave a phantom, draggable separator
        # pinned to the far-right edge.
        self.setCentralWidget(None)

        self.scene_panel = ScenePanel()
        self.render_panel = RenderPanel()
        self.deadline_panel = DeadlinePanel()
        self.queue_panel = QueuePanel()
        self.presets_panel = PresetBrowserPanel()
        self.logs_panel = LogsPanel()
        self.preview_panel = PreviewPanel()

        self.scene_dock = self._mk_dock("Scene", "SceneDock", self.scene_panel)
        self.render_dock = self._mk_dock("Render Settings", "RenderDock", self.render_panel)
        self.deadline_dock = self._mk_dock("Deadline Farm", "DeadlineDock", self.deadline_panel)
        self.queue_dock = self._mk_dock("Queue", "QueueDock", self.queue_panel)
        self.presets_dock = self._mk_dock("Presets", "PresetsDock", self.presets_panel)
        self.logs_dock = self._mk_dock("Live Logs", "LogsDock", self.logs_panel)
        self.logs_dock.setMinimumHeight(60)
        self.preview_dock = self._mk_dock("Live Preview", "PreviewDock", self.preview_panel)

        self._all_docks = (
            self.scene_dock, self.render_dock, self.deadline_dock, self.queue_dock,
            self.presets_dock, self.logs_dock, self.preview_dock,
        )
        self._apply_layout("default")

        self.view_menu.addSeparator()
        for d in (self.scene_dock, self.render_dock, self.deadline_dock, self.queue_dock, self.presets_dock, self.logs_dock, self.preview_dock):
            self.view_menu.addAction(d.toggleViewAction())

        self.scene_panel.scan_requested.connect(self._scan_scene)
        self.scene_panel.videos_changed.connect(self._on_videos_changed)
        self.scene_panel.assignments_changed.connect(self._on_assignments_changed)
        self.scene_panel.auto_mapped.connect(
            lambda n, total: self._append_log(
                f"[app] Auto-mapped {n} of {total} materials by name"
                + ("" if n else " — no filenames matched a material name")))
        self.scene_panel.watch_status.connect(lambda msg: self._append_log(f"[app] {msg}"))
        self.scene_panel.watch_changed.connect(lambda *_: (self._save_profile(), self._update_status_bar()))
        self.scene_panel.target_set_ready.connect(self._on_target_set_ready)
        self.scene_panel.assignments_cleared.connect(
            lambda snap: self._push_undo(f"Clear Mappings ({len(snap)})",
                                         lambda: self.scene_panel.set_assignments(snap)))
        self.scene_panel.mute_changed.connect(self._schedule_save)
        self.scene_panel.render_requested.connect(self._start_render)

        self.render_panel.output_changed.connect(lambda _v: self._on_settings_changed())
        self.render_panel.tokens_requested.connect(self._show_output_tokens_menu)
        self.render_panel.width_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.height_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.fps_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.frame_start_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.frame_end_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.frame_start_edit.textChanged.connect(lambda _v: self._sync_preview_frame_range())
        self.render_panel.frame_end_edit.textChanged.connect(lambda _v: self._sync_preview_frame_range())
        self._sync_preview_frame_range()  # seed the picker from the default range
        self.render_panel.frame_step_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.engine_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.samples_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.denoise_cb.stateChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.view_transform_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.exposure_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.gamma_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.device_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.scale_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.quality_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.codec_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.transparent_cb.stateChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_preset_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_min_samples_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_threshold_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_gi_bounces_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_ray_depth_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_gi_cb.toggled.connect(lambda _v: self._on_settings_changed())

        self.deadline_panel.settings_changed.connect(lambda: self._on_settings_changed())
        self.deadline_panel.test_connection_requested.connect(self._test_deadline_connection)
        self.deadline_panel.export_requested.connect(self._export_deadline_files)

        self.render_panel.profile_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.scene_panel.scene_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.scene_panel.camera_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())

        self.queue_panel.queue_requested.connect(self._queue_current_jobs)
        self.queue_panel.start_selected_requested.connect(lambda: self._start_render(render_all=False))
        self.queue_panel.start_all_requested.connect(lambda: self._start_render(render_all=True))
        self.queue_panel.cancel_requested.connect(self._cancel_render)
        self.queue_panel.skip_requested.connect(self._skip_render)
        self.queue_panel.job_selected.connect(self._on_queue_job_selected)
        self.queue_panel.job_run_toggled.connect(self._on_queue_job_run_toggled)
        self.queue_panel.remove_jobs_requested.connect(self._remove_queue_jobs)
        self.queue_panel.remove_selected_requested.connect(self._remove_selected_queue_rows)
        self.queue_panel.reveal_output_requested.connect(self._reveal_job_output)
        self.queue_panel.open_output_requested.connect(self._open_job_output)
        self.queue_panel.move_job_requested.connect(self._move_job)
        self.queue_panel.duplicate_jobs_requested.connect(self._duplicate_jobs)
        self.queue_panel.job_renamed.connect(self._on_job_renamed)
        self.queue_panel.clear_queue_requested.connect(self._clear_queue)

        self.presets_panel.save_requested.connect(self._save_preset)
        self.presets_panel.load_requested.connect(self._load_preset_entry)
        self.presets_panel.apply_selected_requested.connect(lambda e: self._apply_preset_to_queue(e, checked_only=False))
        self.presets_panel.apply_checked_requested.connect(lambda e: self._apply_preset_to_queue(e, checked_only=True))
        self.presets_panel.delete_requested.connect(self._delete_preset_entry)
        self.presets_panel.refresh_requested.connect(self._refresh_preset_browser)
        self.presets_panel.open_folder_requested.connect(self._open_presets_folder)
        self._refresh_preset_browser()

        self.logs_panel.copy_diag.connect(self._copy_diagnostics)
        self.preview_panel.preview_frame_requested.connect(self._render_preview_frame)

    def _mk_dock(self, title: str, obj_name: str, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(obj_name)
        # Custom QWidget subclasses ignore their stylesheet background unless
        # WA_StyledBackground is set — without this the panel paints nothing and
        # you see through it (e.g. to the panel behind it in a tab stack).
        widget.setAttribute(Qt.WA_StyledBackground, True)
        dock.setWidget(widget)
        # Standard dock behaviour: movable, floatable and closable. Closable is
        # what lets the View-menu toggleViewAction() entries enable/check; without
        # it Qt greys them out because the panel can never be hidden.
        dock.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        # When a dock gets tabbed / untabbed / floated, re-evaluate whether its
        # title bar should be hidden (tabbed → tab is the header) or shown.
        dock.dockLocationChanged.connect(lambda *_: self._schedule_titlebar_sync())
        dock.topLevelChanged.connect(lambda *_: self._schedule_titlebar_sync())
        dock.visibilityChanged.connect(lambda *_: self._schedule_titlebar_sync())
        return dock

    def _schedule_titlebar_sync(self) -> None:
        if getattr(self, "_titlebar_sync_pending", False):
            return
        self._titlebar_sync_pending = True
        QTimer.singleShot(0, self._sync_dock_titlebars)

    def _make_solo_tab_titlebar(self, dock: QDockWidget) -> QWidget:
        """A title bar that renders the panel name as a single left-aligned tab,
        so a standalone panel looks just like one tab in a tabbed group."""
        bar = QWidget(dock)
        bar.setObjectName("SoloTabBar")
        bar.setProperty("kind", "solo")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        chip = QLabel(dock.windowTitle(), bar)
        chip.setObjectName("SoloTab")
        lay.addWidget(chip, 0, Qt.AlignLeft | Qt.AlignBottom)
        lay.addStretch(1)
        return bar

    def _sync_dock_titlebars(self) -> None:
        """Make every panel header read as a left-aligned tab. A dock sharing a
        tab group with another visible dock has its title bar hidden (the real
        tab bar is its header); a standalone/floating dock gets a single 'solo'
        tab so the look is consistent whether or not it's tabbed."""
        self._titlebar_sync_pending = False
        for d in getattr(self, "_all_docks", ()):  # may run before docks exist
            others = [o for o in self.tabifiedDockWidgets(d) if o.isVisible()]
            tabbed = bool(others) and d.isVisible() and not d.isFloating()
            cur = d.titleBarWidget()
            kind = cur.property("kind") if cur is not None else None
            if tabbed:
                if kind != "empty":
                    w = QWidget(d)
                    w.setProperty("kind", "empty")  # collapses the title bar
                    d.setTitleBarWidget(w)
            else:
                if kind != "solo":
                    d.setTitleBarWidget(self._make_solo_tab_titlebar(d))

        # Real tab bars (tabbed groups): natural width + left-aligned, and drop
        # the base frame/strip Qt otherwise draws behind the tabs.
        for tb in self.findChildren(QTabBar):
            if tb.expanding():
                tb.setExpanding(False)
            if tb.drawBase():
                tb.setDrawBase(False)
            if not tb.documentMode():
                tb.setDocumentMode(True)

    # ── Guided flow / health bar ─────────────────────────────────────────
    def _build_flowbar(self) -> None:
        bar = QToolBar("Workflow Status", self)
        bar.setObjectName("FlowBar")
        bar.setMovable(False)
        bar.setFloatable(False)
        self.addToolBar(Qt.TopToolBarArea, bar)

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(12, 3, 12, 3)
        row.setSpacing(8)

        self._flow_steps: dict[str, tuple[QLabel, QLabel]] = {}
        chips = (("blender", "Blender"), ("scene", "Scene"), ("map", "Mapping"), ("queue", "Queue"))
        for i, (key, text) in enumerate(chips):
            if i:
                sep = QLabel("›")
                sep.setObjectName("HintLabel")
                row.addWidget(sep)
            chip = QWidget()
            ch = QHBoxLayout(chip)
            ch.setContentsMargins(0, 0, 0, 0)
            ch.setSpacing(6)
            icon_lbl = QLabel()
            icon_lbl.setFixedSize(16, 16)
            txt_lbl = QLabel(text)
            txt_lbl.setObjectName("FieldLabel")
            ch.addWidget(icon_lbl)
            ch.addWidget(txt_lbl)
            row.addWidget(chip)
            self._flow_steps[key] = (icon_lbl, txt_lbl)
        row.addStretch()
        bar.addWidget(container)

    def _update_health(self) -> None:
        if not hasattr(self, "_flow_steps"):
            return
        pal = self._palette

        def set_chip(key: str, state: str, text: str) -> None:
            icon_lbl, txt_lbl = self._flow_steps[key]
            color = {"ok": pal.success, "warn": pal.warning, "idle": pal.text_faint}[state]
            name = {"ok": "check", "warn": "alert", "idle": "circle"}[state]
            icon_lbl.setPixmap(icons.pixmap(name, color, 15))
            txt_lbl.setText(text)

        blender = _find_blender(self._blender_path)
        set_chip("blender", "ok" if blender else "warn", "Blender ready" if blender else "No Blender")

        scene = self.scene_panel.scene_edit.text().strip()
        if scene and file_exists(scene):
            set_chip("scene", "ok", Path(scene).name)
        else:
            set_chip("scene", "idle", "No scene")

        n_map = len(self.scene_panel.get_assignments())
        n_vid = len(self.scene_panel.get_videos())
        if n_map:
            set_chip("map", "ok", f"{n_map} mapping{'s' if n_map != 1 else ''}")
        elif n_vid:
            set_chip("map", "idle", f"{n_vid} video{'s' if n_vid != 1 else ''}")
        else:
            set_chip("map", "idle", "No videos")

        n_job = len(self._jobs)
        set_chip("queue", "ok" if n_job else "idle", f"{n_job} job{'s' if n_job != 1 else ''}")

    # ── Toast notifications ──────────────────────────────────────────────
    def _show_toast(self, message: str, kind: str = "info") -> None:
        # Pop-up notifications were removed by request. Messages are routed to
        # the Live Logs panel instead so feedback is still available without an
        # on-screen overlay.
        prefix = {"error": "[error] ", "warning": "[warn] "}.get(kind, "[app] ")
        self._append_log(prefix + message)

    def _position_toast(self, toast: QWidget) -> None:
        margin = 26
        x = self.width() - toast.width() - margin
        y = self.height() - toast.height() - margin
        toast.move(max(margin, x), max(margin, y))

    def _dismiss_toast(self, toast: QWidget) -> None:
        if toast is None:
            return
        eff = toast.graphicsEffect()
        if eff is None:
            toast.deleteLater()
            if toast is self._toast:
                self._toast = None
            return
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(320)
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.finished.connect(toast.deleteLater)
        anim.start()
        self._toast_anim = anim
        if toast is self._toast:
            self._toast = None

    def _reset_layout(self) -> None:
        self._apply_layout("default")
        self._apply_default_geometry()
        self._schedule_save()

    def _apply_layout(self, preset: str) -> None:
        """Deterministically (re)build the dock layout for a named preset.

        Every dock is first detached (removeDockWidget) and re-added so no
        stale split/tabify relationships survive — this is what makes repeated
        resets and preset switches robust instead of collapsing docks
        off-screen.
        """
        sc, rd, dl, q, pr, lg, pv = (
            self.scene_dock, self.render_dock, self.deadline_dock, self.queue_dock,
            self.presets_dock, self.logs_dock, self.preview_dock,
        )
        for d in self._all_docks:
            d.setFloating(False)
            self.removeDockWidget(d)
        for d in self._all_docks:
            self.addDockWidget(Qt.LeftDockWidgetArea, d)
            d.show()

        if preset == "grid":
            # Every panel visible at once, none tabbed — three columns each
            # split vertically so all seven docks are on screen together.
            #   col1: Scene / Presets
            #   col2: Render Settings / Deadline Farm
            #   col3: Queue / Live Preview / Live Logs
            self.splitDockWidget(sc, rd, Qt.Horizontal)
            self.splitDockWidget(rd, q, Qt.Horizontal)
            self.splitDockWidget(sc, pr, Qt.Vertical)
            self.splitDockWidget(rd, dl, Qt.Vertical)
            self.splitDockWidget(q, pv, Qt.Vertical)
            self.splitDockWidget(pv, lg, Qt.Vertical)
            self.resizeDocks([sc, rd, q], [440, 440, 560], Qt.Horizontal)
            self.resizeDocks([sc, pr], [480, 320], Qt.Vertical)
            self.resizeDocks([rd, dl], [480, 320], Qt.Vertical)
            self.resizeDocks([q, pv, lg], [360, 320, 180], Qt.Vertical)
        elif preset == "focus":
            # Big render-monitoring layout: Scene+Render left, Queue+Preview
            # large on the right, Logs underneath.
            self.splitDockWidget(sc, q, Qt.Horizontal)
            self.splitDockWidget(sc, rd, Qt.Vertical)
            self.splitDockWidget(q, lg, Qt.Vertical)
            self.tabifyDockWidget(rd, dl)
            self.tabifyDockWidget(rd, pr)
            self.tabifyDockWidget(q, pv)
            rd.raise_(); pv.raise_()
            self.resizeDocks([sc, q], [430, 870], Qt.Horizontal)
            self.resizeDocks([q, lg], [640, 220], Qt.Vertical)
        elif preset == "setup":
            # Configuration-focused: Scene + Render Settings side by side and
            # large; the render/monitor docks tabbed along the bottom.
            self.splitDockWidget(sc, rd, Qt.Horizontal)
            self.splitDockWidget(sc, q, Qt.Vertical)
            self.tabifyDockWidget(sc, dl)
            self.tabifyDockWidget(q, pr)
            self.tabifyDockWidget(q, lg)
            self.tabifyDockWidget(q, pv)
            sc.raise_(); q.raise_()
            self.resizeDocks([sc, rd], [620, 620], Qt.Horizontal)
            self.resizeDocks([sc, q], [560, 240], Qt.Vertical)
        elif preset == "tabbed":
            # Single pane: every panel tabbed into one stack, maximising the
            # working area of whichever panel is active.
            for d in (rd, dl, q, pr, lg, pv):
                self.tabifyDockWidget(sc, d)
            sc.raise_()
        elif preset == "stacked":
            # Two columns: Scene left, everything else tabbed on the right.
            self.splitDockWidget(sc, rd, Qt.Horizontal)
            for d in (dl, q, pr, lg, pv):
                self.tabifyDockWidget(rd, d)
            rd.raise_()
            self.resizeDocks([sc, rd], [430, 1040], Qt.Horizontal)
        else:  # "default" — three columns
            self.splitDockWidget(sc, rd, Qt.Horizontal)
            self.splitDockWidget(rd, q, Qt.Horizontal)
            self.splitDockWidget(rd, pr, Qt.Vertical)
            self.splitDockWidget(q, lg, Qt.Vertical)
            self.tabifyDockWidget(rd, dl)
            self.tabifyDockWidget(q, pv)
            rd.raise_(); q.raise_()
            self.resizeDocks([sc, rd, q], [380, 480, 620], Qt.Horizontal)
            self.resizeDocks([rd, pr], [520, 240], Qt.Vertical)
            self.resizeDocks([q, lg], [640, 200], Qt.Vertical)

        self._current_layout = preset
        self._schedule_titlebar_sync()

    def _apply_default_geometry(self) -> None:
        """Default window: 70% of the screen's available area, centered."""
        scr = self.screen() or QApplication.primaryScreen()
        if scr is None:
            return
        a = scr.availableGeometry()
        self.resize(int(a.width() * 0.7), int(a.height() * 0.7))
        fg = self.frameGeometry()
        fg.moveCenter(a.center())
        self.move(fg.topLeft())

    def _init_window_geometry(self) -> None:
        """On first show: keep the user's remembered size/position if it's
        sensible; otherwise (first run, or a stale near-fullscreen geometry)
        use the 70%-centered default."""
        scr = self.screen() or QApplication.primaryScreen()
        if scr is None:
            return
        a = scr.availableGeometry()
        fg = self.frameGeometry()
        inter = a.intersected(fg)
        # Treat the remembered geometry as usable only if it's substantially
        # on-screen and not near-fullscreen; otherwise use the 70% default.
        mostly_on_screen = inter.width() >= fg.width() * 0.6 and inter.height() >= fg.height() * 0.6
        near_fullscreen = fg.width() >= a.width() * 0.92 or fg.height() >= a.height() * 0.92
        if self._restored_geometry and mostly_on_screen and not near_fullscreen:
            self._fit_to_screen(recenter=False)
        else:
            self._apply_default_geometry()

    def _fit_to_screen(self, recenter: bool = False) -> None:
        """Keep the window within the current screen's available area, and
        optionally re-center it. Prevents the window from growing past the
        bottom of the screen when switching dense layouts."""
        scr = self.screen() or QApplication.primaryScreen()
        if scr is None:
            return
        a = scr.availableGeometry()
        w = min(self.width(), a.width())
        h = min(self.height(), a.height())
        if w != self.width() or h != self.height():
            self.resize(w, h)
        fg = self.frameGeometry()
        off_screen = (
            fg.top() < a.top() or fg.left() < a.left()
            or fg.bottom() > a.bottom() or fg.right() > a.right()
        )
        if recenter or off_screen:
            fg.moveCenter(a.center())
            fg.moveTop(max(a.top(), min(fg.top(), a.bottom() - fg.height())))
            fg.moveLeft(max(a.left(), min(fg.left(), a.right() - fg.width())))
            self.move(fg.topLeft())

    def _apply_named_layout(self, preset: str) -> None:
        self._apply_layout(preset)
        self._fit_to_screen(recenter=False)
        self._schedule_save()

    def _save_custom_layout(self) -> None:
        self._custom_layout_state = bytes(self.saveState().toBase64()).decode("ascii")
        if hasattr(self, "restore_layout_action"):
            self.restore_layout_action.setEnabled(True)
        self._schedule_save()
        self._show_toast("Layout saved", "success")

    def _restore_custom_layout(self) -> None:
        if not self._custom_layout_state:
            return
        try:
            self.restoreState(QByteArray.fromBase64(self._custom_layout_state.encode("ascii")))
            self._fit_to_screen(recenter=False)
            self._schedule_titlebar_sync()
        except Exception:
            self._show_toast("Could not restore layout", "warning")

    def event(self, e):  # type: ignore[override]
        if e.type() == QEvent.LayoutRequest:
            self._schedule_save()
        return super().event(e)

    def _append_log(self, line: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {line}"
        self.logs_panel.append(line)
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            # Rotate log if it exceeds max size
            if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
                backup = LOG_PATH.with_suffix(".1.log")
                if backup.exists():
                    backup.unlink()
                LOG_PATH.rename(backup)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def _init_blender(self) -> None:
        b = _find_blender(self._blender_path)
        if b:
            self._blender_path = b
            self._append_log(f"[app] Blender detected: {b}")
        else:
            self._append_log("[app] Blender not found. Use Profile -> Properties.")
            self._prompt_install_runtime()
        self._update_health()

        # Auto-scan a restored scene so materials/cameras are ready on launch.
        scene = self.scene_panel.scene_edit.text().strip()
        if b and scene and file_exists(scene) and not self._discovered_materials:
            self._append_log("[app] Auto-scanning restored scene…")
            self._scan_scene()

    def _ensure_c4dpy(self, interactive: bool = False) -> str:
        if self._c4dpy_path and Path(self._c4dpy_path).exists():
            return self._c4dpy_path
        found = _find_c4dpy()
        if found:
            self._c4dpy_path = found
            return found
        if interactive:
            QMessageBox.warning(
                self, "Cinema 4D Not Found",
                "Couldn't find Cinema 4D's headless Python (c4dpy).\n\n"
                "Install Cinema 4D (2023+) to render .c4d scenes, or use a "
                ".blend / FBX / USD scene instead.")
        return ""

    def _ensure_blender(self, interactive: bool = False) -> Optional[str]:
        b = _find_blender(self._blender_path)
        if b:
            self._blender_path = b
            return b
        if not interactive:
            return None
        ans = QMessageBox.question(
            self,
            "Blender Not Found",
            "Blender is missing. Install managed Blender runtime now?\n\n"
            "Choose No to locate Blender manually.",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            QMessageBox.Yes,
        )
        if ans == QMessageBox.Yes:
            self._install_managed_runtime()
            return None
        if ans == QMessageBox.No:
            self._show_properties_dialog()
        return _find_blender(self._blender_path)

    def _prompt_install_runtime(self) -> None:
        if self._runtime_prompted:
            return
        self._runtime_prompted = True
        ans = QMessageBox.question(
            self,
            "Install Blender Runtime",
            "No Blender installation was found.\n"
            "Would you like to download and install a managed Blender runtime now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if ans == QMessageBox.Yes:
            self._install_managed_runtime()

    def _install_managed_runtime(self) -> None:
        if self._runtime_install_thread and self._runtime_install_thread.isRunning():
            QMessageBox.information(self, "Runtime", "Runtime installation is already in progress.")
            return

        self._append_log(f"[runtime] Installing Blender {BLENDER_RUNTIME_VERSION}...")
        self._runtime_install_thread = RuntimeInstallThread(self)
        self._runtime_install_thread.log.connect(self._append_log)
        self._runtime_install_thread.finished_install.connect(self._on_runtime_installed)
        self._runtime_install_thread.start()

    def _on_runtime_installed(self, blender_path: str, error: str) -> None:
        if error:
            self._append_log(f"[runtime] Install failed: {error}")
            QMessageBox.warning(self, "Runtime Install Failed", error)
            return
        self._blender_path = blender_path
        self._append_log(f"[runtime] Installed Blender runtime: {blender_path}")
        self._schedule_save()
        self._update_health()
        self._show_toast("Managed Blender runtime is ready", "success")

    def _show_properties_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Properties & Settings")
        dlg.setMinimumWidth(720)
        dlg.setMinimumHeight(460)
        root = QVBoxLayout(dlg)
        tabs = QTabWidget()
        root.addWidget(tabs)

        def section_title(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setObjectName("DialogSection")
            return lbl

        def _tab(title: str) -> QVBoxLayout:
            page = QWidget()
            v = QVBoxLayout(page)
            v.setSpacing(10)
            tabs.addTab(page, title)
            return v

        def _open_path(target) -> None:
            target = Path(target)
            try:
                target.mkdir(parents=True, exist_ok=True)
                if sys.platform == "darwin":
                    subprocess.Popen(["open", str(target)])
                elif os.name == "nt":
                    os.startfile(str(target))  # type: ignore[attr-defined]
                else:
                    subprocess.Popen(["xdg-open", str(target)])
            except Exception:
                pass

        def hint(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color:{self._palette.text_muted}; font-size:11px;")
            return lbl

        # ── General ──────────────────────────────────────────────────────
        lay = _tab("General")
        lay.addWidget(section_title("WHEN A RENDER FINISHES"))
        behave_row = QHBoxLayout()
        behave_row.addWidget(QLabel("Then:"))
        when_combo = QComboBox()
        _when_opts = [("Do nothing", "nothing"), ("Quit the app", "quit"), ("Sleep the computer", "sleep")]
        when_combo.addItems([lbl for lbl, _ in _when_opts])
        _vals = [v for _, v in _when_opts]
        when_combo.setCurrentIndex(_vals.index(self._when_done) if self._when_done in _vals else 0)
        behave_row.addWidget(when_combo)
        behave_row.addStretch()
        lay.addLayout(behave_row)

        lay.addWidget(section_title("PREVIEW"))
        preview_cb = QCheckBox("Show a live frame preview while rendering")
        preview_cb.setChecked(self._preview_enabled)
        lay.addWidget(preview_cb)
        lay.addWidget(hint("Renders the current frame as it goes so you can watch progress. "
                           "Turn off for a small speed-up on heavy scenes."))
        lay.addStretch()

        # ── Render Engines ───────────────────────────────────────────────
        lay = _tab("Render Engines")
        lay.addWidget(hint("Scenes route to a renderer automatically by type — Blender for "
                           ".blend / .fbx / .usd / .obj…, and Cinema 4D + Redshift for .c4d."))

        lay.addWidget(section_title("BLENDER"))
        blender_row = QHBoxLayout()
        blender_edit = QLineEdit(self._blender_path)
        blender_edit.setPlaceholderText("Path to the Blender executable")
        blender_locate = QPushButton("Locate")
        blender_row.addWidget(QLabel("Executable:"))
        blender_row.addWidget(blender_edit, 1)
        blender_row.addWidget(blender_locate)
        lay.addLayout(blender_row)

        def do_locate_blender() -> None:
            if sys.platform == "darwin":
                chosen = QFileDialog.getExistingDirectory(dlg, "Select Blender.app", "/Applications")
            else:
                chosen, _ = QFileDialog.getOpenFileName(dlg, "Select Blender executable")
            if chosen:
                resolved = _norm_blender(chosen)
                if resolved:
                    blender_edit.setText(resolved)
                else:
                    QMessageBox.warning(dlg, "Invalid", "Could not find a Blender executable there.")
        blender_locate.clicked.connect(do_locate_blender)

        detect_row = QHBoxLayout()
        detect_btn = QPushButton("Auto-detect")
        detect_btn.setToolTip("Search the usual install locations for Blender")
        install_btn = QPushButton("Install Managed Blender…")
        install_btn.setToolTip(f"Download a self-contained Blender {BLENDER_RUNTIME_VERSION} runtime")
        detect_row.addWidget(detect_btn)
        detect_row.addWidget(install_btn)
        detect_row.addStretch()
        lay.addLayout(detect_row)
        blender_ver_lbl = hint("")
        lay.addWidget(blender_ver_lbl)

        def do_check_version() -> None:
            exe = blender_edit.text().strip()
            if not exe or not Path(exe).exists():
                blender_ver_lbl.setText("No valid Blender path set.")
                return
            try:
                out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15)
                text = (out.stdout or out.stderr).strip()
                first = text.splitlines()[0] if text else ""
                blender_ver_lbl.setText(f"✓ {first}" if first else "Could not read Blender version.")
            except Exception as exc:
                blender_ver_lbl.setText(f"Version check failed: {exc}")

        def do_autodetect() -> None:
            found = _find_blender(blender_edit.text().strip())
            if found:
                blender_edit.setText(found)
                do_check_version()
            else:
                blender_ver_lbl.setText("No Blender found in the usual locations.")

        detect_btn.clicked.connect(do_autodetect)
        install_btn.clicked.connect(self._install_managed_runtime)
        blender_edit.editingFinished.connect(do_check_version)
        do_check_version()

        lay.addWidget(section_title("CINEMA 4D + REDSHIFT"))
        c4d_row = QHBoxLayout()
        c4dpy_edit = QLineEdit(self._c4dpy_path)
        c4dpy_edit.setPlaceholderText("Path to c4dpy (Cinema 4D's headless Python)")
        c4d_locate = QPushButton("Locate")
        c4d_row.addWidget(QLabel("c4dpy:"))
        c4d_row.addWidget(c4dpy_edit, 1)
        c4d_row.addWidget(c4d_locate)
        lay.addLayout(c4d_row)
        c4d_detect_row = QHBoxLayout()
        c4d_detect_btn = QPushButton("Auto-detect")
        c4d_detect_btn.setToolTip("Search the usual install locations for Cinema 4D's c4dpy")
        c4d_detect_row.addWidget(c4d_detect_btn)
        c4d_detect_row.addStretch()
        lay.addLayout(c4d_detect_row)
        c4d_status_lbl = hint("")

        def _c4d_refresh() -> None:
            p = c4dpy_edit.text().strip()
            if p and Path(p).exists():
                c4d_status_lbl.setText(f"✓ Cinema 4D detected — {Path(p).name}")
            else:
                c4d_status_lbl.setText("Not set — only needed for Cinema 4D (.c4d) scenes. Redshift renders use this.")

        def do_locate_c4d() -> None:
            chosen, _ = QFileDialog.getOpenFileName(dlg, "Select c4dpy executable", c4dpy_edit.text() or "")
            if chosen:
                c4dpy_edit.setText(chosen)
                _c4d_refresh()

        def do_detect_c4d() -> None:
            found = _find_c4dpy()
            if found:
                c4dpy_edit.setText(found)
            _c4d_refresh()

        c4d_locate.clicked.connect(do_locate_c4d)
        c4d_detect_btn.clicked.connect(do_detect_c4d)
        c4dpy_edit.editingFinished.connect(_c4d_refresh)
        lay.addWidget(c4d_status_lbl)
        _c4d_refresh()
        lay.addStretch()

        # ── Watch & Auto-render ──────────────────────────────────────────
        lay = _tab("Watch && Auto-render")
        lay.addWidget(section_title("WATCH FOLDER"))
        lay.addWidget(hint("Drop clips into a watch folder and they import + map by name "
                           "automatically (latest version wins)."))
        _wi, _ws = self.scene_panel.get_watch_options()
        watch_row = QHBoxLayout()
        watch_interval_edit = QLineEdit(f"{_wi / 1000:g}")
        watch_interval_edit.setFixedWidth(70)
        watch_interval_edit.setToolTip("How often the watch folder is polled, in seconds.")
        watch_settle_edit = QLineEdit(f"{_ws:g}")
        watch_settle_edit.setFixedWidth(70)
        watch_settle_edit.setToolTip("How long a file's size must stay steady before it's imported "
                                     "(guards against half-copied files).")
        watch_row.addWidget(QLabel("Poll interval (s):"))
        watch_row.addWidget(watch_interval_edit)
        watch_row.addSpacing(16)
        watch_row.addWidget(QLabel("Stability window (s):"))
        watch_row.addWidget(watch_settle_edit)
        watch_row.addStretch()
        lay.addLayout(watch_row)

        lay.addWidget(section_title("AUTO-RENDER"))
        ar_enable_cb = QCheckBox("Auto-render the mapped screens when their clips change")
        ar_enable_cb.setChecked(self._autorender_enabled)
        ar_enable_cb.setToolTip("Mapping a clip to a material targets it. When the mapped set is complete "
                                "— or newer versions arrive — a single render covering every mapped screen "
                                "is queued automatically (debounced, so a batch becomes one render).")
        lay.addWidget(ar_enable_cb)
        lay.addWidget(hint("Mapping a clip to a material includes it — there's nothing else to mark. "
                           "Newer versions of any mapped clip re-trigger the render."))
        ar_start_cb = QCheckBox("Start it automatically (otherwise just add it to the queue)")
        ar_start_cb.setChecked(self._autorender_start)
        lay.addWidget(ar_start_cb)
        ar_out_row = QHBoxLayout()
        ar_out_edit = QLineEdit(self._autorender_output)
        ar_out_edit.setPlaceholderText("Output folder (blank = a PREVIZ subfolder of the watch folder)")
        ar_out_browse = QPushButton("Browse")
        def _pick_ar_out() -> None:
            d = QFileDialog.getExistingDirectory(dlg, "Auto-render output folder", ar_out_edit.text() or str(Path.home()))
            if d:
                ar_out_edit.setText(d)
        ar_out_browse.clicked.connect(_pick_ar_out)
        ar_out_row.addWidget(QLabel("Output:"))
        ar_out_row.addWidget(ar_out_edit, 1)
        ar_out_row.addWidget(ar_out_browse)
        lay.addLayout(ar_out_row)
        ar_pat_row = QHBoxLayout()
        ar_pat_edit = QLineEdit(self._autorender_pattern)
        ar_pat_edit.setToolTip("Output filename. Tokens: {clip} (first mapped clip), {scene}, {date}.")
        ar_pat_row.addWidget(QLabel("Name:"))
        ar_pat_row.addWidget(ar_pat_edit, 1)
        ar_pat_hint = QLabel("tokens: {clip} {scene} {date}")
        ar_pat_hint.setStyleSheet(f"color:{self._palette.text_muted}; font-size:11px;")
        ar_pat_row.addWidget(ar_pat_hint)
        lay.addLayout(ar_pat_row)
        lay.addStretch()

        # ── Updates ──────────────────────────────────────────────────────
        lay = _tab("Updates")
        lay.addWidget(section_title("SOFTWARE UPDATES"))
        lay.addWidget(hint("Updates are automatic — the app checks for a newer release on launch "
                           "and offers a one-click download. Nothing to configure."))
        upd_status = QLabel(f"This build is v{APP_VERSION}.")
        upd_status.setStyleSheet(f"color:{self._palette.text_muted}; font-size:12px;")
        lay.addWidget(upd_status)
        upd_check_btn = QPushButton("Check for Updates Now")
        upd_check_btn.clicked.connect(lambda: self._check_for_updates(manual=True))
        upd_row = QHBoxLayout()
        upd_row.addWidget(upd_check_btn)
        upd_row.addStretch()
        lay.addLayout(upd_row)
        if not _update_token():
            lay.addWidget(hint("This build has no update token baked in, so automatic checks are off."))
        lay.addStretch()

        # ── Deadline ─────────────────────────────────────────────────────
        lay = _tab("Deadline")
        lay.addWidget(hint("Submit Blender and Cinema 4D jobs to a Thinkbox Deadline farm. "
                           "Leave blank to render locally."))
        lay.addWidget(section_title("CONFIGURATION"))

        # Repo Path
        repo_row = QHBoxLayout()
        repo_edit = QLineEdit(self._deadline_repo_path)
        repo_edit.setPlaceholderText("Default repository (or browse path)")
        repo_locate = QPushButton("Locate")
        repo_row.addWidget(QLabel("Repository Path:"))
        repo_row.addWidget(repo_edit, 1)
        repo_row.addWidget(repo_locate)
        lay.addLayout(repo_row)

        def do_locate_repo() -> None:
            chosen = QFileDialog.getExistingDirectory(dlg, "Select Deadline Repository Path", repo_edit.text() or "")
            if chosen:
                repo_edit.setText(chosen)
        repo_locate.clicked.connect(do_locate_repo)

        # Command Path
        cmd_row = QHBoxLayout()
        cmd_edit = QLineEdit(self._deadline_command_path)
        cmd_edit.setPlaceholderText("Path to deadlinecommand executable (optional)")
        cmd_locate = QPushButton("Locate")
        cmd_row.addWidget(QLabel("Command Path:   "))
        cmd_row.addWidget(cmd_edit, 1)
        cmd_row.addWidget(cmd_locate)
        lay.addLayout(cmd_row)

        def do_locate_cmd() -> None:
            chosen, _ = QFileDialog.getOpenFileName(dlg, "Select deadlinecommand executable", cmd_edit.text() or "")
            if chosen:
                cmd_edit.setText(chosen)
        cmd_locate.clicked.connect(do_locate_cmd)

        # Name Template
        template_row = QHBoxLayout()
        template_edit = QLineEdit(self._deadline_job_name_template)
        template_edit.setPlaceholderText("e.g. BlenderRender Job - {scene_name}")
        template_row.addWidget(QLabel("Name Template:  "))
        template_row.addWidget(template_edit, 1)
        lay.addLayout(template_row)

        # Comment
        comment_row = QHBoxLayout()
        comment_edit = QLineEdit(self._deadline_comment)
        comment_edit.setPlaceholderText("Optional job comment")
        comment_row.addWidget(QLabel("Job Comment:    "))
        comment_row.addWidget(comment_edit, 1)
        lay.addLayout(comment_row)

        lay.addWidget(section_title("CONNECTION"))

        status_lbl = QLabel("Connection status: Not tested")
        status_lbl.setStyleSheet(f"color: {self._palette.text_faint}; font-size: 11px; font-weight: bold;")
        lay.addWidget(status_lbl)

        # Buttons Row
        diag_btn_layout = QHBoxLayout()
        test_conn_btn = QPushButton("Test Connection")
        export_files_btn = QPushButton("Export Job Files")
        diag_btn_layout.addWidget(test_conn_btn)
        diag_btn_layout.addWidget(export_files_btn)
        diag_btn_layout.addStretch()
        lay.addLayout(diag_btn_layout)
        lay.addStretch()

        # ── About ────────────────────────────────────────────────────────
        lay = _tab("About")
        title = QLabel(APP_NAME)
        title.setStyleSheet(f"color:{self._palette.text}; font-size:18px; font-weight:700;")
        lay.addWidget(title)
        lay.addWidget(hint("Map videos onto a 3D scene's screens and render them headlessly — "
                           "Blender or Cinema 4D + Redshift, locally or on a Deadline farm."))
        meta = QLabel(f"Version {APP_VERSION}   ·   {platform.system()} {platform.machine()}   ·   "
                      f"Python {platform.python_version()}")
        meta.setStyleSheet(f"color:{self._palette.text_muted}; font-size:11px;")
        lay.addWidget(meta)

        lay.addWidget(section_title("BUNDLED TOOLS"))
        ff_lbl = QLabel(f"ffmpeg:   {find_ffmpeg_tool('ffmpeg') or 'not found'}\n"
                        f"ffprobe:  {_find_ffprobe() or 'not found'}")
        ff_lbl.setStyleSheet(f"color:{self._palette.text_muted}; font-size:11px;")
        ff_lbl.setWordWrap(True)
        lay.addWidget(ff_lbl)

        lay.addWidget(section_title("FILES & LOGS"))
        data_dir = PROFILE_PATH.parent
        data_row = QHBoxLayout()
        data_row.addWidget(QLabel("App data folder:"))
        data_path = QLineEdit(str(data_dir))
        data_path.setReadOnly(True)
        data_row.addWidget(data_path, 1)
        open_data_btn = QPushButton("Open")
        open_data_btn.clicked.connect(lambda: _open_path(data_dir))
        data_row.addWidget(open_data_btn)
        lay.addLayout(data_row)
        diag_tools = QHBoxLayout()
        open_log_btn = QPushButton("Open Logs Folder")
        open_log_btn.clicked.connect(lambda: _open_path(LOG_PATH.parent))
        copy_diag_btn = QPushButton("Copy Diagnostics")
        copy_diag_btn.clicked.connect(self._copy_diagnostics)
        diag_tools.addWidget(open_log_btn)
        diag_tools.addWidget(copy_diag_btn)
        diag_tools.addStretch()
        lay.addLayout(diag_tools)
        lay.addStretch()

        # Powered-by branding (logo image if present in assets/, else styled text).
        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color:{self._palette.border};")
        lay.addWidget(sep)
        powered_lbl = QLabel("Powered by")
        powered_lbl.setStyleSheet(f"color:{self._palette.text_faint}; font-size:11px;")
        lay.addWidget(powered_lbl)
        _logo = self._logo_path()
        _logo_pm = QPixmap(str(_logo)) if _logo is not None else None
        if _logo_pm is not None and not _logo_pm.isNull():
            scaled = _logo_pm.scaledToWidth(240, Qt.SmoothTransformation)  # 2× of 120 for Retina
            scaled.setDevicePixelRatio(2.0)
            lay.addWidget(_ImageView(scaled, self._palette.window), 0, Qt.AlignLeft)
        else:
            brand_lbl = QLabel("Toy Robot Media")
            brand_lbl.setStyleSheet(f"color:{self._palette.text}; font-size:14px; font-weight:700;")
            lay.addWidget(brand_lbl)

        # Dialog buttons live below the tabs.
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        root.addWidget(btns)

        # Handle Ok/Cancel
        def on_accept() -> None:
            self._blender_path = blender_edit.text().strip()
            self._c4dpy_path = c4dpy_edit.text().strip()
            self._deadline_repo_path = repo_edit.text().strip()
            self._deadline_command_path = cmd_edit.text().strip()
            self._deadline_job_name_template = template_edit.text().strip()
            self._deadline_comment = comment_edit.text().strip()
            
            # Sync hidden widgets in panel if needed
            self.deadline_panel.dl_cmd_edit.setText(self._deadline_command_path)
            self.deadline_panel.dl_repo_edit.setText(self._deadline_repo_path)
            self.deadline_panel.dl_name_template_edit.setText(self._deadline_job_name_template)
            self.deadline_panel.dl_comment_edit.setText(self._deadline_comment)

            # Behaviour
            self._when_done = _vals[when_combo.currentIndex()]
            _menu_label = {"nothing": "Do Nothing", "quit": "Quit App", "sleep": "Sleep Computer"}.get(self._when_done)
            if _menu_label and hasattr(self, "_when_actions") and _menu_label in self._when_actions:
                self._when_actions[_menu_label].setChecked(True)
            if hasattr(self, "preview_action"):
                self.preview_action.setChecked(preview_cb.isChecked())
            else:
                self._preview_enabled = preview_cb.isChecked()

            # Watch / ingest options
            def _to_float(s, d):
                try:
                    return float(s)
                except ValueError:
                    return d
            interval_ms = int(max(1.0, _to_float(watch_interval_edit.text().strip(), 3.0)) * 1000)
            settle_s = max(0.0, _to_float(watch_settle_edit.text().strip(), 2.0))
            self.scene_panel.set_watch_options(interval_ms, settle_s)

            # Auto-render (targets)
            self._autorender_enabled = ar_enable_cb.isChecked()
            self._autorender_start = ar_start_cb.isChecked()
            self._autorender_output = ar_out_edit.text().strip()
            self._autorender_pattern = ar_pat_edit.text().strip() or "{clip}_PREVIZ"

            self._schedule_save()
            dlg.accept()

        btns.accepted.connect(on_accept)
        btns.rejected.connect(dlg.reject)

        # Handle testing connection and exporting files inside dialog
        def run_test_connection() -> None:
            cmd = cmd_edit.text().strip()
            if not cmd:
                cmd = find_deadlinecommand() or "deadlinecommand"
            
            status_lbl.setText("Connection status: Testing...")
            status_lbl.setStyleSheet(f"color: {self._palette.warning}; font-size: 11px; font-weight: bold;")
            QApplication.processEvents()

            if not Path(cmd).exists() and not shutil.which(cmd):
                status_lbl.setText("Connection status: deadlinecommand not found")
                status_lbl.setStyleSheet(f"color: {self._palette.danger}; font-size: 11px; font-weight: bold;")
                QMessageBox.critical(dlg, "Deadline Connection Error", f"deadlinecommand not found at {cmd}.\nPlease check your Thinkbox Deadline installation.")
                return

            repo = repo_edit.text().strip()
            if repo:
                p_args = [cmd, "RunCommandForRepository", "Direct", repo, "-pools"]
                g_args = [cmd, "RunCommandForRepository", "Direct", repo, "-groups"]
                m_args = [cmd, "RunCommandForRepository", "Direct", repo, "-GetSlaveNames"]
            else:
                p_args = [cmd, "-pools"]
                g_args = [cmd, "-groups"]
                m_args = [cmd, "-GetSlaveNames"]

            try:
                p_res = subprocess.run(p_args, capture_output=True, text=True, timeout=8)
                g_res = subprocess.run(g_args, capture_output=True, text=True, timeout=8)
                m_res = subprocess.run(m_args, capture_output=True, text=True, timeout=8)

                if p_res.returncode == 0:
                    pools = [line.strip() for line in p_res.stdout.splitlines() if line.strip()]
                    current_pool = self.deadline_panel.dl_pool_combo.currentText()
                    current_sec_pool = self.deadline_panel.dl_sec_pool_combo.currentText()
                    self.deadline_panel.dl_pool_combo.clear()
                    self.deadline_panel.dl_sec_pool_combo.clear()
                    self.deadline_panel.dl_pool_combo.addItems(pools)
                    self.deadline_panel.dl_sec_pool_combo.addItems([""] + pools)
                    if current_pool:
                        self.deadline_panel.dl_pool_combo.setCurrentText(current_pool)
                    if current_sec_pool:
                        self.deadline_panel.dl_sec_pool_combo.setCurrentText(current_sec_pool)

                if g_res.returncode == 0:
                    groups = [line.strip() for line in g_res.stdout.splitlines() if line.strip()]
                    current_group = self.deadline_panel.dl_group_combo.currentText()
                    self.deadline_panel.dl_group_combo.clear()
                    self.deadline_panel.dl_group_combo.addItems([""] + groups)
                    if current_group:
                        self.deadline_panel.dl_group_combo.setCurrentText(current_group)

                if m_res.returncode == 0:
                    machines = [line.strip() for line in m_res.stdout.splitlines() if line.strip()]
                    self.deadline_panel.dl_machines_list.blockSignals(True)
                    currently_checked = set()
                    for i in range(self.deadline_panel.dl_machines_list.count()):
                        item = self.deadline_panel.dl_machines_list.item(i)
                        if item.checkState() == Qt.Checked:
                            currently_checked.add(item.text().strip())

                    self.deadline_panel.dl_machines_list.clear()
                    for m in machines:
                        item = QListWidgetItem(m)
                        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                        if m in currently_checked or not currently_checked:
                            item.setCheckState(Qt.Checked)
                        else:
                            item.setCheckState(Qt.Unchecked)
                        self.deadline_panel.dl_machines_list.addItem(item)
                    self.deadline_panel.dl_machines_list.blockSignals(False)

                if p_res.returncode == 0:
                    status_lbl.setText("Connection status: Connected")
                    status_lbl.setStyleSheet(f"color: {self._palette.success}; font-size: 11px; font-weight: bold;")
                    QMessageBox.information(dlg, "Deadline Connection", "Successfully connected to Deadline repository and updated pools, groups, and machine list!")
                else:
                    status_lbl.setText("Connection status: Connection failed")
                    status_lbl.setStyleSheet(f"color: {self._palette.danger}; font-size: 11px; font-weight: bold;")
                    QMessageBox.warning(dlg, "Deadline Warning", f"deadlinecommand returned exit code {p_res.returncode}.\nStderr: {p_res.stderr.strip()}")
            except Exception as exc:
                status_lbl.setText(f"Connection status: Error ({type(exc).__name__})")
                status_lbl.setStyleSheet(f"color: {self._palette.danger}; font-size: 11px; font-weight: bold;")
                QMessageBox.critical(dlg, "Deadline Connection Error", f"Failed to communicate with Deadline:\n{exc}")

        test_conn_btn.clicked.connect(run_test_connection)
        export_files_btn.clicked.connect(self._export_deadline_files)

        dlg.exec()

    def _add_recent_scene(self, path: str) -> None:
        p = str(Path(os.path.expanduser(path.strip())))
        if not p:
            return
        self._recent_scenes = [p] + [s for s in self._recent_scenes if s != p]
        self._recent_scenes = self._recent_scenes[:12]
        self.scene_panel.set_recent_scenes(self._recent_scenes)
        self._schedule_save()

    def _scan_scene(self) -> None:
        scene = self.scene_panel.scene_edit.text().strip()
        if not scene or not file_exists(scene):
            return
        is_c4d = scene.lower().endswith(".c4d")
        # Cinema 4D scenes need c4dpy; everything else needs Blender.
        if is_c4d:
            c4dpy = self._ensure_c4dpy(interactive=True)
            if not c4dpy:
                return
            blender = self._blender_path
        else:
            blender = self._ensure_blender(interactive=True)
            if not blender:
                return
        self._add_recent_scene(scene)
        if self._scan_in_progress:
            return

        self._scan_in_progress = True
        self.scene_panel.scan_btn.setEnabled(False)
        self._append_log("[app] Scanning scene...")

        try:
            script = _resolve_runtime_script("blender_discover.py")
            c4d_script = _resolve_runtime_script("c4d_discover.py") if is_c4d else ""
        except Exception as exc:
            self._append_log(f"[app] ERROR: {exc}")
            self.scene_panel.scan_btn.setEnabled(True)
            self._scan_in_progress = False
            return

        self._discovery_thread = DiscoveryThread(
            blender, script, scene,
            c4dpy=(self._c4dpy_path if is_c4d else ""), c4d_script=c4d_script)
        self._discovery_thread.log.connect(self._append_log)
        self._discovery_thread.discovered.connect(self._on_discovery)
        self._discovery_thread.error.connect(self._on_discovery_error)
        self._discovery_thread.finished.connect(self._on_discovery_done)
        self._discovery_thread.start()

    def _build_status_bar(self) -> None:
        """A bottom status bar summarising scene / renderer / mappings / queue /
        watch state at a glance, plus the version."""
        from PySide6.QtWidgets import QStatusBar
        sb = QStatusBar()
        sb.setSizeGripEnabled(False)
        self.setStatusBar(sb)
        self._sb_scene = QLabel("")
        self._sb_renderer = QLabel("")
        self._sb_map = QLabel("")
        self._sb_queue = QLabel("")
        self._sb_watch = QLabel("")
        muted = self._palette.text_muted
        for w in (self._sb_scene, self._sb_renderer, self._sb_map, self._sb_queue, self._sb_watch):
            w.setStyleSheet(f"color:{muted}; padding:0 10px;")
            sb.addWidget(w)
        spacer = QLabel("")
        sb.addWidget(spacer, 1)
        self._sb_update = QLabel("")
        self._sb_update.setStyleSheet(f"color:{self._palette.accent_text}; padding:0 10px; font-weight:600;")
        sb.addPermanentWidget(self._sb_update)
        ver = QLabel(f"v{APP_VERSION}")
        ver.setStyleSheet(f"color:{self._palette.text_faint}; padding:0 10px;")
        sb.addPermanentWidget(ver)

    def _update_status_bar(self) -> None:
        if not hasattr(self, "_sb_scene"):
            return
        scene = self.scene_panel.scene_edit.text().strip()
        self._sb_scene.setText(f"Scene: {Path(scene).name if scene else '—'}")
        self._sb_renderer.setText(f"Renderer: {self.render_panel.engine_combo.currentText() or '—'}")
        self._sb_map.setText(f"Mapped: {len(self.scene_panel.get_assignments())}")
        self._sb_queue.setText(f"Queue: {len(self._jobs)}")
        try:
            _f, watching = self.scene_panel.get_watch_folder()
        except Exception:
            watching = False
        self._sb_watch.setText("Watch: on" if watching else "")

    # ── Undo for destructive actions ─────────────────────────────────────
    def _push_undo(self, desc: str, restore) -> None:
        self._undo_stack.append((desc, restore))
        del self._undo_stack[:-15]   # keep the last 15
        if hasattr(self, "_undo_action"):
            self._undo_action.setEnabled(True)
            self._undo_action.setText(f"Undo {desc}")

    def _undo(self) -> None:
        if not self._undo_stack:
            return
        desc, restore = self._undo_stack.pop()
        try:
            restore()
            self._append_log(f"[app] Undid: {desc}")
        except Exception as exc:
            self._append_log(f"[app] Undo failed: {exc}")
        if hasattr(self, "_undo_action"):
            nxt = self._undo_stack[-1][0] if self._undo_stack else ""
            self._undo_action.setEnabled(bool(self._undo_stack))
            self._undo_action.setText(f"Undo {nxt}" if nxt else "Undo")

    # ── Updates (automatic: checks GitHub Releases with a baked-in read-only
    #    token, so a private repo updates with zero per-machine config) ────────
    _ASSET_FOR_PLATFORM = {
        "macos-arm64": "RenderMapperPro-macOS-arm64.zip",
        "macos-intel": "RenderMapperPro-macOS-intel.zip",
        "windows-x64": "RenderMapperPro-Windows-x64.zip",
    }

    def _check_for_updates(self, manual: bool = False) -> None:
        token = _update_token()
        if not token:
            if manual:
                QMessageBox.information(self, "Updates",
                    "Automatic updates aren't configured in this build "
                    "(no update token was baked in).")
            return

        def work():
            info = None
            try:
                import urllib.request
                req = urllib.request.Request(
                    f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                    headers={"Authorization": f"Bearer {token}",
                             "Accept": "application/vnd.github+json",
                             "X-GitHub-Api-Version": "2022-11-28",
                             "User-Agent": APP_NAME})
                with urllib.request.urlopen(req, timeout=15) as r:
                    info = json.loads(r.read().decode("utf-8"))
            except Exception:
                info = None
            self._update_checked.emit(info, manual)

        threading.Thread(target=work, daemon=True).start()

    def _on_update_checked(self, info, manual: bool) -> None:
        if not info:
            if manual:
                QMessageBox.information(self, "Updates", "Couldn't reach GitHub to check for updates.")
            return
        tag = str(info.get("tag_name", "")).strip()
        if not tag or _version_tuple(tag) <= _version_tuple(APP_VERSION):
            self._sb_update.setText("")
            if manual:
                QMessageBox.information(self, "Updates", f"You're up to date (v{APP_VERSION}).")
            return
        self._sb_update.setText(f"● Update {tag} available")
        self._offer_update(info, tag)

    def _offer_update(self, info, tag: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle("Update Available")
        box.setText(f"{APP_NAME} {tag} is available — you have v{APP_VERSION}.")
        notes = str(info.get("body") or "").strip()
        if notes:
            box.setInformativeText(notes[:600] + ("…" if len(notes) > 600 else ""))
        get = box.addButton("Download Update", QMessageBox.AcceptRole)
        box.addButton("Later", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is get:
            self._fetch_update(info)

    def _fetch_update(self, info) -> None:
        want = self._ASSET_FOR_PLATFORM.get(_update_platform_key())
        asset = next((a for a in info.get("assets", []) if a.get("name") == want), None)
        if not asset:
            QMessageBox.warning(self, "Update",
                f"This release has no build for your platform ({_update_platform_key()}).")
            return
        token = _update_token()
        dest = Path.home() / "Downloads" / want
        try:
            import urllib.request
            req = urllib.request.Request(asset["url"], headers={
                "Authorization": f"Bearer {token}", "Accept": "application/octet-stream",
                "User-Agent": APP_NAME})
            dest.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(req, timeout=300) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            import zipfile
            with zipfile.ZipFile(dest) as z:
                z.extractall(dest.parent)
            reveal_in_file_manager(dest)
            QMessageBox.information(self, "Update Downloaded",
                f"{want} was downloaded to your Downloads and unzipped.\n\n"
                f"Quit {APP_NAME} and replace it with the new build, then reopen.")
        except Exception as exc:
            QMessageBox.warning(self, "Update Failed", str(exc))

    def _set_renderer_options(self, is_c4d: bool, detected: str = "") -> None:
        """Populate the renderer dropdown with the engines that apply to the
        loaded scene: Cinema 4D renderers for a .c4d, Blender engines otherwise."""
        self.render_panel.set_renderer(is_c4d)   # adapt all settings to the renderer
        combo = self.render_panel.engine_combo
        # C4D path only supports Redshift: the video→emission mapping is a
        # Redshift node-material feature. Standard/Physical use legacy material
        # channels and are not wired. Blender keeps its own engines.
        items = ["Redshift"] if is_c4d else ["CYCLES", "BLENDER_EEVEE"]
        if [combo.itemText(i) for i in range(combo.count())] == items:
            return
        cur = combo.currentText()
        target = detected if detected in items else (cur if cur in items else items[0])
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(items)
        idx = combo.findText(target)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _on_discovery(self, materials: list, cameras: list, settings: dict) -> None:
        clean_materials = [str(m).strip() for m in materials if str(m).strip()]
        clean_cameras = [str(c).strip() for c in cameras if str(c).strip()]
        self._discovered_materials = clean_materials
        self._discovered_cameras = clean_cameras

        self.scene_panel.set_materials(clean_materials)
        self.scene_panel.set_cameras(clean_cameras, self.scene_panel.camera_combo.currentText())

        current = self.scene_panel.get_assignments()
        current = [a for a in current if a.material_name in set(clean_materials)]
        self.scene_panel.set_assignments(current)

        # The renderer dropdown reflects the scene type: C4D renderers for a
        # .c4d, Blender engines otherwise.
        is_c4d = self.scene_panel.scene_edit.text().strip().lower().endswith(".c4d")
        self._set_renderer_options(is_c4d, settings.get("renderer", "") if settings else "")

        # Pull render/timeline/colour settings from the scene into the UI. Guard
        # so the per-field edits don't each fire _on_settings_changed; we sync
        # once at the end.
        if settings:
            self._loading_job_into_ui = True
            try:
                self.render_panel.apply_scene_settings(settings)
            finally:
                self._loading_job_into_ui = False
            self._sync_preview_frame_range()   # preview slider gets the scene range
            keys = ", ".join(k for k in ("fps", "frame_start", "frame_end", "width", "height", "samples", "engine") if k in settings)
            self._append_log(f"[app] Applied scene settings ({keys})")

        # Re-read the loaded clips from disk: audio badges AND each file's
        # fps/length, so a re-exported video is reflected. When clips are loaded
        # they drive the timeline (fps + frame range); the scene still supplies
        # resolution / engine / samples / colour.
        self.scene_panel.refresh_videos()
        self._loading_job_into_ui = True
        try:
            self._reprobe_loaded_videos()
        finally:
            self._loading_job_into_ui = False
        self._sync_preview_frame_range()

        self._append_log(f"[app] Discovery complete: {len(clean_materials)} materials, {len(clean_cameras)} cameras")
        if not clean_cameras:
            self._append_log("[app] No cameras discovered in scene")
        self._refresh_job_outputs()
        # Push the freshly-applied settings onto the active job, then save.
        if settings and self._active_job_id is not None:
            self._on_settings_changed()
        else:
            self._refresh_queue_view()

    def _on_discovery_error(self, err: str) -> None:
        self._append_log(f"[app] Discovery ERROR: {err}")
        QMessageBox.critical(self, "Scan Failed", err)

    def _on_discovery_done(self) -> None:
        self._scan_in_progress = False
        self.scene_panel.scan_btn.setEnabled(True)

    def _reprobe_loaded_videos(self) -> None:
        """Re-read the loaded video files from disk and update fps + frame range
        from the primary clip, so a re-exported / swapped video is picked up on
        refresh. No-op when no clips are loaded (then the scene drives the range)."""
        videos = self.scene_panel.get_videos()
        if not videos:
            return
        info = _parse_mp4_info(videos[0])
        if not info:
            return
        frames, raw_fps = info
        fps = _normalize_fps(raw_fps, 30)
        self.render_panel.fps_edit.setText(str(fps))
        self.render_panel.frame_start_edit.setText("1")
        if frames and frames > 0:
            self.render_panel.frame_end_edit.setText(str(frames))
        self._append_log(f"[app] Re-read video {Path(videos[0]).name}: {frames} frames @ {fps} fps")

    def _on_videos_changed(self, videos: list[str]) -> None:
        new_video = next((v for v in videos if v not in self._known_videos), None)
        self._known_videos = set(videos)

        if new_video:
            info = _parse_mp4_info(new_video)
            if info:
                frames, raw_fps = info
                self.render_panel.fps_edit.setText(str(_normalize_fps(raw_fps, 30)))
                self.render_panel.frame_start_edit.setText("1")
                if frames and frames > 0:
                    self.render_panel.frame_end_edit.setText(str(frames))
            else:
                # Couldn't read the video — fall back to a sane default fps.
                self.render_panel.fps_edit.setText("30")
            # Default output name from the imported video: <name>_PREVIZ_v001
            # (only when the user hasn't already set an output path).
            if not self.render_panel.output_edit.text().strip():
                self.render_panel.output_edit.setText(self._default_preview_output(new_video))

            # One-time nudge: without ffprobe, FPS/frame detection falls back to a
            # rough parser and only MP4/MOV get audio badges.
            if not self._ffmpeg_hint_shown and _find_ffprobe() is None:
                self._ffmpeg_hint_shown = True
                self._show_toast(
                    "Install ffmpeg for accurate FPS detection and audio badges on "
                    "all formats — e.g. “brew install ffmpeg”.",
                    "warning",
                )

        self._sync_active_job_from_scene()
        self._refresh_job_outputs()
        self._refresh_queue_view()
        self._schedule_save()

    def _on_assignments_changed(self, _asn: list[MaterialVideoAssignment]) -> None:
        # Auto-draft: the moment there's a real mapping, materialise it as a live
        # job so edits are always captured and switching jobs never loses work.
        self._ensure_active_job()
        self._sync_active_job_from_scene()
        self._refresh_job_outputs()
        self._refresh_queue_view()
        self._schedule_save()
        self._request_auto_preview()

    def _ensure_active_job(self) -> None:
        """If there's a scene + a mapping but no active job, create one and make
        it active. This removes the 'floating unsaved changes' state entirely —
        from here on edits live-save into this job (auto-draft)."""
        if self._active_job_id is not None or self._loading_job_into_ui:
            return
        if not self.scene_panel.scene_edit.text().strip():
            return
        assignments = self.scene_panel.get_assignments()
        if not assignments:
            return
        job = RenderJob(id=self._next_job_id)
        self._next_job_id += 1
        job.video_path = assignments[0].video_path
        self._make_job_snapshot(job, assignments)
        job.label = self._derive_job_label(job, f"Mapped scene ({len(assignments)} materials)")
        self._jobs.append(job)
        self._active_job_id = job.id

    def _on_target_set_ready(self, assignments: list) -> None:
        """All target screens have clips (and the set changed) — queue one
        multi-screen render named after the clips with a PREVIZ suffix."""
        if not self._autorender_enabled or not assignments:
            return
        scene = self.scene_panel.scene_edit.text().strip()
        if not scene:
            return
        job = RenderJob(id=self._next_job_id)
        self._next_job_id += 1
        job.video_path = assignments[0].video_path
        self._make_job_snapshot(job, assignments)

        primary = Path(assignments[0].video_path).stem
        base = (self._autorender_pattern or "{clip}_PREVIZ") \
            .replace("{clip}", primary) \
            .replace("{scene}", Path(scene).stem) \
            .replace("{date}", datetime.now().strftime("%Y-%m-%d"))
        out_fmt, _codec = OUTPUT_PROFILES.get(job.output_profile or "H264 MP4", ("MPEG4", "H264"))
        ext = ext_for_format(out_fmt) or ".mp4"
        watch_folder, _en = self.scene_panel.get_watch_folder()
        out_dir = self._autorender_output or (
            os.path.join(watch_folder, "PREVIZ") if watch_folder
            else str(Path(assignments[0].video_path).parent / "PREVIZ"))
        self.scene_panel.set_watch_ignore_dir(out_dir)   # never re-ingest our own renders
        job.output_path = str(Path(out_dir) / f"{base}{ext}")
        job.output_input = job.output_path
        job.custom_label = True
        job.label = f"Auto · {base}"
        self._jobs.insert(0, job)
        self._refresh_queue_view()
        self._schedule_save()
        screens = ", ".join(a.material_name for a in assignments)
        self._append_log(f"[app] Auto-render queued: {job.label}  ({len(assignments)} screens: {screens})")
        if self._autorender_start and not self._is_rendering:
            self._start_render(only_job_ids={job.id})   # start just this auto-render job

    def _request_auto_preview(self) -> None:
        """Debounced trigger: when Auto is on, re-render the preview a moment
        after the user stops changing settings/mappings."""
        if getattr(self, "_loading_job_into_ui", False):
            return
        pp = getattr(self, "preview_panel", None)
        if pp is None or not pp.auto_btn.isChecked():
            return
        t = getattr(self, "_auto_preview_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._fire_auto_preview)
            self._auto_preview_timer = t
        t.start(700)

    def _fire_auto_preview(self) -> None:
        if not self.preview_panel.auto_btn.isChecked() or self._is_rendering:
            return
        if not self._preview_ready():
            return
        # _render_preview_frame coalesces if a preview is already running.
        self._render_preview_frame()

    def _audio_paths_for(self, assignments: list[MaterialVideoAssignment]) -> list[str]:
        """Audio sources to mux for a job: every mapped clip that carries an
        audio stream and the user hasn't muted, de-duplicated in map order."""
        sp = self.scene_panel
        seen: set[str] = set()
        out: list[str] = []
        for a in assignments:
            vp = a.video_path
            if vp and vp not in seen and video_has_audio(vp) and not sp.is_muted(vp):
                seen.add(vp)
                out.append(vp)
        return out

    def _show_output_tokens_menu(self) -> None:
        """Cinema-style token picker for the Output Path — each entry shows a
        live preview and inserts the token at the cursor when chosen."""
        sp, rp = self.scene_panel, self.render_panel
        assignments = sp.get_assignments()
        videos = sp.get_videos()
        video_src = assignments[0].video_path if assignments else (videos[0] if videos else "")
        video_name = Path(video_src).stem if video_src else "video"
        scene_name = Path(sp.scene_edit.text().strip()).stem or "scene"
        camera = sp.camera_combo.currentText().strip() or "camera"
        w = rp.width_edit.text().strip() or "1920"
        h = rp.height_edit.text().strip() or "1080"
        fps = rp.fps_edit.text().strip() or "30"
        today = datetime.now().strftime("%Y-%m-%d")

        items = [
            ("{video}", "First video name", video_name),
            ("{scene}", "3D scene name", scene_name),
            ("{camera}", "Camera name", camera),
            ("{res}", "Resolution", f"{w}x{h}"),
            ("{width}", "Width", w),
            ("{height}", "Height", h),
            ("{fps}", "Frame rate", fps),
            ("{date}", "Date", today),
        ]
        menu = QMenu(self)
        for token, label, preview in items:
            act = menu.addAction(f"{label}   —   {preview}")
            act.setToolTip(token)
            act.triggered.connect(lambda _c=False, t=token: self._insert_output_token(t))
        menu.exec(rp.tokens_btn.mapToGlobal(rp.tokens_btn.rect().bottomLeft()))

    def _insert_output_token(self, token: str) -> None:
        edit = self.render_panel.output_edit
        edit.insert(token)
        edit.setFocus()

    @staticmethod
    def _default_preview_output(video_path: str) -> str:
        """Default render output next to the source video:
        <video name>_PREVIZ_v001.mp4, picking the next free version number."""
        p = Path(video_path)
        d = p.parent if str(p.parent) not in ("", ".") else Path.home()
        n = 1
        while (d / f"{p.stem}_PREVIZ_v{n:03d}.mp4").exists():
            n += 1
        return str(d / f"{p.stem}_PREVIZ_v{n:03d}.mp4")

    def _sync_active_job_from_scene(self) -> None:
        """Keep the active queued job in lock-step with the current scene
        mappings/videos so the queue auto-updates as the user edits (silent)."""
        if self._active_job_id is None:
            return
        job = next((j for j in self._jobs if j.id == self._active_job_id), None)
        if job is None:
            return
        assignments = self.scene_panel.get_assignments()
        if assignments:
            job.video_path = assignments[0].video_path
        self._make_job_snapshot(job, assignments)
        fallback = (
            f"Mapped scene ({len(job.material_assignments)} materials)"
            if job.material_assignments else (Path(job.video_path).name or f"Job {job.id}")
        )
        job.label = self._derive_job_label(job, fallback)

    def _derive_job_label(self, job: RenderJob, fallback: str) -> str:
        # A hand-typed name always wins and is never auto-overwritten.
        if getattr(job, "custom_label", False) and (job.label or "").strip():
            return job.label
        for raw in ((job.output_input or "").strip(), (job.output_path or "").strip()):
            if not raw:
                continue
            p = Path(raw).expanduser()
            name = p.name.strip()
            if name:
                return name
        # Auto label: tag with the camera so duplicated variations of the same
        # scene are distinguishable at a glance.
        cam = (getattr(job, "target_camera", "") or "").strip()
        return f"{fallback} · {cam}" if cam else fallback

    def _make_job_snapshot(self, job: RenderJob, assignments: list[MaterialVideoAssignment]) -> None:
        job.material_assignments = [MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode) for a in assignments]
        job.scene_path = self.scene_panel.scene_edit.text().strip()
        job.target_camera = self.scene_panel.camera_combo.currentText()
        job.output_input = self.render_panel.output_edit.text().strip()
        job.output_profile = self.render_panel.profile_combo.currentText()
        job.render_options = self.render_panel.render_options()
        job.safe_mode = True
        job.use_deadline = self.deadline_panel.use_dl_cb.isChecked()
        job.deadline_pool = self.deadline_panel.dl_pool_combo.currentText().strip()
        job.deadline_secondary_pool = self.deadline_panel.dl_sec_pool_combo.currentText().strip()
        job.deadline_group = self.deadline_panel.dl_group_combo.currentText().strip()
        job.deadline_priority = self.deadline_panel.dl_prio_spin.value()
        job.deadline_comment = self._deadline_comment
        job.deadline_department = self.deadline_panel.dl_dept_edit.text().strip()
        job.deadline_chunk_size = self.deadline_panel.dl_chunk_spin.value()
        job.deadline_suspended = self.deadline_panel.dl_suspended_cb.isChecked()
        job.deadline_submit_scene = self.deadline_panel.dl_submit_scene_cb.isChecked()
        job.deadline_job_name_template = self._deadline_job_name_template
        job.deadline_machine_limit = self.deadline_panel.dl_machine_limit_spin.value()
        job.deadline_limits = self.deadline_panel.dl_limits_edit.text().strip()
        job.deadline_command_path = self._deadline_command_path
        job.deadline_repo_path = self._deadline_repo_path
        job.deadline_whitelist = self.deadline_panel.get_selected_machines()


    def _sync_jobs(self) -> None:
        assignments = self.scene_panel.get_assignments()
        videos = self.scene_panel.get_videos()

        if assignments:
            existing = next((j for j in self._jobs if j.material_assignments), None)
            if existing is None:
                existing = RenderJob(id=self._next_job_id)
                self._next_job_id += 1
            existing.video_path = assignments[0].video_path
            default_label = f"Mapped scene ({len(assignments)} materials)"
            existing.label = default_label
            self._make_job_snapshot(existing, assignments)
            self._jobs = [existing]
        else:
            existing_map = {j.video_path: j for j in self._jobs if not j.material_assignments}
            synced: list[RenderJob] = []
            for v in videos:
                default_label = Path(v).name
                job = existing_map.get(v)
                if job is None:
                    job = RenderJob(id=self._next_job_id, video_path=v, label=default_label)
                    self._next_job_id += 1
                self._make_job_snapshot(job, [])
                synced.append(job)
            self._jobs = synced

        self._refresh_job_outputs()
        for j in self._jobs:
            fallback = f"Mapped scene ({len(j.material_assignments)} materials)" if j.material_assignments else (Path(j.video_path).name or f"Job {j.id}")
            j.label = self._derive_job_label(j, fallback)
        self._refresh_queue_view()
        self._update_progress_caption()

    def _queue_current_jobs(self) -> None:
        assignments = self.scene_panel.get_assignments()
        videos = self.scene_panel.get_videos()

        to_add: list[RenderJob] = []
        if assignments:
            job = RenderJob(id=self._next_job_id)
            self._next_job_id += 1
            job.video_path = assignments[0].video_path
            default_label = f"Mapped scene ({len(assignments)} materials)"
            job.label = default_label
            self._make_job_snapshot(job, assignments)
            job.label = self._derive_job_label(job, default_label)
            to_add.append(job)
        else:
            for v in videos:
                default_label = Path(v).name
                job = RenderJob(id=self._next_job_id, video_path=v, label=default_label)
                self._next_job_id += 1
                self._make_job_snapshot(job, [])
                job.label = self._derive_job_label(job, default_label)
                to_add.append(job)

        if not to_add:
            QMessageBox.information(self, "Queue", "Nothing to queue. Add videos or assignments first.")
            return

        # New jobs go to the top of the queue.
        self._jobs[:0] = to_add
        self._refresh_job_outputs()
        for j in to_add:
            fallback = f"Mapped scene ({len(j.material_assignments)} materials)" if j.material_assignments else (Path(j.video_path).name or f"Job {j.id}")
            j.label = self._derive_job_label(j, fallback)
        self._active_job_id = to_add[0].id
        self._refresh_queue_view()
        self.queue_panel.select_job(self._active_job_id)
        self._schedule_save()

    def _refresh_queue_view(self) -> None:
        self.queue_panel.set_jobs(self._jobs)
        if self._active_job_id is not None:
            self.queue_panel.select_job(self._active_job_id)
        self._update_health()
        self._update_status_bar()

    def _unsaved_floating_changes(self) -> bool:
        """True when the UI holds a mapped setup that isn't backed by any queued
        job (no active job) — loading another job would silently lose it."""
        return (
            self._active_job_id is None
            and not self._loading_job_into_ui
            and bool(self.scene_panel.scene_edit.text().strip())
            and bool(self.scene_panel.get_assignments())
        )

    def _on_queue_job_selected(self, job_id: int) -> None:
        if getattr(self, "_in_select_guard", False):
            return
        # Defensive: with auto-draft this shouldn't happen, but if any unsaved
        # floating work exists, silently preserve it as a job before switching —
        # never lose work, never interrupt with a dialog.
        if self._unsaved_floating_changes():
            self._in_select_guard = True
            try:
                self._ensure_active_job()
                self._refresh_queue_view()
            finally:
                self._in_select_guard = False

        self._active_job_id = job_id
        job = next((j for j in self._jobs if j.id == job_id), None)
        if not job:
            return
        self._loading_job_into_ui = True
        try:
            self.scene_panel.scene_edit.setText(job.scene_path or "")
            if job.target_camera:
                idx = self.scene_panel.camera_combo.findText(job.target_camera)
                if idx >= 0:
                    self.scene_panel.camera_combo.setCurrentIndex(idx)
                else:
                    if self.scene_panel.camera_combo.count() > 1:
                        self.scene_panel.camera_combo.setCurrentIndex(1)
                    else:
                        self.scene_panel.camera_combo.setCurrentIndex(0)
            else:
                if self.scene_panel.camera_combo.count() > 1:
                    self.scene_panel.camera_combo.setCurrentIndex(1)
                else:
                    self.scene_panel.camera_combo.setCurrentIndex(0)

            opts = job.render_options or self.render_panel.render_options()
            self.render_panel.width_edit.setText(str(opts.width))
            self.deadline_panel.use_dl_cb.setChecked(getattr(job, 'use_deadline', False))
            self.deadline_panel.dl_pool_combo.setCurrentText(getattr(job, 'deadline_pool', ""))
            self.deadline_panel.dl_sec_pool_combo.setCurrentText(getattr(job, 'deadline_secondary_pool', ""))
            self.deadline_panel.dl_group_combo.setCurrentText(getattr(job, 'deadline_group', ""))
            self.deadline_panel.dl_prio_spin.setValue(getattr(job, 'deadline_priority', 50))
            self.deadline_panel.dl_comment_edit.setText(self._deadline_comment)
            self.deadline_panel.dl_dept_edit.setText(getattr(job, 'deadline_department', ""))
            self.deadline_panel.dl_chunk_spin.setValue(getattr(job, 'deadline_chunk_size', 1))
            self.deadline_panel.dl_suspended_cb.setChecked(getattr(job, 'deadline_suspended', False))
            self.deadline_panel.dl_submit_scene_cb.setChecked(getattr(job, 'deadline_submit_scene', True))
            self.deadline_panel.dl_name_template_edit.setText(self._deadline_job_name_template)
            self.deadline_panel.dl_machine_limit_spin.setValue(getattr(job, 'deadline_machine_limit', 0))
            self.deadline_panel.dl_limits_edit.setText(getattr(job, 'deadline_limits', ""))
            self.deadline_panel.dl_cmd_edit.setText(self._deadline_command_path)
            self.deadline_panel.dl_repo_edit.setText(self._deadline_repo_path)
            self.deadline_panel.set_selected_machines(getattr(job, 'deadline_whitelist', ""))
            self.render_panel.height_edit.setText(str(opts.height))
            self.render_panel.fps_edit.setText(str(opts.fps))
            self.render_panel.frame_start_edit.setText(str(opts.frame_start))
            self.render_panel.frame_end_edit.setText(str(opts.frame_end))
            self.render_panel.frame_step_edit.setText(str(opts.frame_step))
            self.render_panel.samples_edit.setText(str(getattr(opts, "samples", 64)))
            self.render_panel.denoise_cb.setChecked(bool(getattr(opts, "use_denoise", True)))
            self.render_panel.view_transform_combo.setCurrentText(getattr(opts, "color_view_transform", "AgX"))
            self.render_panel.exposure_edit.setText(str(getattr(opts, "color_exposure", 0.0)))
            self.render_panel.gamma_edit.setText(str(getattr(opts, "color_gamma", 1.0)))
            _dev_rev = {"AUTO": "Auto", "GPU": "GPU", "CPU": "CPU"}
            self.render_panel.device_combo.setCurrentText(_dev_rev.get(getattr(opts, "device", "AUTO"), "Auto"))
            self.render_panel.scale_combo.setCurrentText(f"{getattr(opts, 'resolution_percentage', 100)}%")
            _q_rev = {"LOSSLESS": "Lossless", "HIGH": "High", "MEDIUM": "Medium", "LOW": "Low", "LOWEST": "Lowest"}
            self.render_panel.quality_combo.setCurrentText(_q_rev.get(getattr(opts, "video_quality", "HIGH"), "High"))
            _c_rev = {"": "Default", "H264": "H.264", "H265": "H.265"}
            self.render_panel.codec_combo.setCurrentText(_c_rev.get(getattr(opts, "video_codec", ""), "Default"))
            self.render_panel.transparent_cb.setChecked(bool(getattr(opts, "film_transparent", False)))

            eidx = self.render_panel.engine_combo.findText(opts.engine)
            if eidx >= 0:
                self.render_panel.engine_combo.setCurrentIndex(eidx)

            pidx = self.render_panel.profile_combo.findText(job.output_profile or "H264 MP4")
            if pidx >= 0:
                self.render_panel.profile_combo.setCurrentIndex(pidx)

            self.render_panel.output_edit.setText(job.output_input or "")
        finally:
            self._loading_job_into_ui = False

    def _on_settings_changed(self) -> None:
        # Auto-preview works off the live UI, so trigger it regardless of whether
        # there's an active queue job (a camera/resolution change should refresh
        # the preview even before anything is queued).
        self._update_status_bar()
        self._request_auto_preview()
        if self._loading_job_into_ui or self._active_job_id is None:
            return
        job = next((j for j in self._jobs if j.id == self._active_job_id), None)
        if not job:
            return
        self._make_job_snapshot(job, list(job.material_assignments))
        # Editing a finished job reactivates it (so the edit re-renders).
        if job.status in ("success", "failed", "cancelled"):
            job.status = "idle"
            job.progress = 0.0
            job.error = ""
            job.selected = True
        self._refresh_job_outputs()
        fallback = f"Mapped scene ({len(job.material_assignments)} materials)" if job.material_assignments else (Path(job.video_path).name or f"Job {job.id}")
        job.label = self._derive_job_label(job, fallback)
        self._refresh_queue_view()
        self._schedule_save()

    def _on_job_renamed(self, job_id: int, new_name: str) -> None:
        job = next((j for j in self._jobs if j.id == job_id), None)
        name = (new_name or "").strip()
        if not job or not name or name == job.label:
            return
        job.label = name
        job.custom_label = True   # never auto-overwrite a hand-typed name
        self._refresh_queue_view()
        self._schedule_save()

    def _on_queue_job_run_toggled(self, job_id: int, selected: bool) -> None:
        job = next((j for j in self._jobs if j.id == job_id), None)
        if not job:
            return
        job.selected = selected
        # Re-checking a completed job reactivates it for another render.
        if selected and job.status in ("success", "failed", "cancelled"):
            job.status = "idle"
            job.progress = 0.0
            job.error = ""
            self._refresh_queue_view()
        self._schedule_save()

    def _remove_queue_jobs(self, job_ids: list[int]) -> None:
        if self._is_rendering:
            QMessageBox.information(self, "Render In Progress", "Stop rendering before removing queue items.")
            return
        ids = {jid for jid in job_ids if isinstance(jid, int)}
        if not ids:
            return

        import copy
        removed = [(i, copy.deepcopy(j)) for i, j in enumerate(self._jobs) if j.id in ids]
        prev_active = self._active_job_id

        def _restore():
            for i, j in removed:
                self._jobs.insert(min(i, len(self._jobs)), j)
            self._active_job_id = prev_active
            self._refresh_queue_view()
            self._schedule_save()
        self._push_undo(f"Delete {len(removed)} job(s)", _restore)

        self._jobs = [j for j in self._jobs if j.id not in ids]

        if self._active_job_id in ids:
            self._active_job_id = self._jobs[0].id if self._jobs else None

        self._refresh_queue_view()
        self._schedule_save()

    def _remove_selected_queue_rows(self) -> None:
        self._remove_queue_jobs(self.queue_panel.selected_row_job_ids())

    def _clear_queue(self) -> None:
        if self._is_rendering:
            QMessageBox.information(self, "Render In Progress", "Stop rendering before clearing the queue.")
            return
        if not self._jobs:
            return
        resp = QMessageBox.question(
            self, "Clear Queue",
            f"Remove all {len(self._jobs)} job(s) from the queue?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if resp != QMessageBox.Yes:
            return
        import copy
        snap_jobs, snap_active = copy.deepcopy(self._jobs), self._active_job_id

        def _restore():
            self._jobs = snap_jobs
            self._active_job_id = snap_active
            self._refresh_queue_view()
            self._schedule_save()
        self._push_undo(f"Clear Queue ({len(self._jobs)} jobs)", _restore)
        self._jobs = []
        self._active_job_id = None
        self._refresh_queue_view()
        self._schedule_save()

    def _job_output_target(self, job_id: int) -> Optional[Path]:
        job = next((j for j in self._jobs if j.id == job_id), None)
        if not job or not (job.output_path or "").strip():
            return None
        p = Path(job.output_path).expanduser()
        if not p.exists():
            p = p if p.suffix else p
            if not p.exists():
                return None
        return p

    def _reveal_job_output(self, job_id: int) -> None:
        p = self._job_output_target(job_id)
        if p is None:
            self._show_toast("Output not found yet", "warning")
            return
        try:
            reveal_in_file_manager(p)
        except Exception as exc:
            self._append_log(f"[app] Reveal failed: {exc}")

    def _open_job_output(self, job_id: int) -> None:
        p = self._job_output_target(job_id)
        if p is None:
            self._show_toast("Output not found yet", "warning")
            return
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            elif os.name == "nt":
                os.startfile(str(p))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as exc:
            self._append_log(f"[app] Open failed: {exc}")

    def _move_job(self, job_id: int, delta: int) -> None:
        if self._is_rendering:
            return
        idx = next((i for i, j in enumerate(self._jobs) if j.id == job_id), -1)
        if idx < 0:
            return
        new = idx + delta
        if new < 0 or new >= len(self._jobs):
            return
        self._jobs[idx], self._jobs[new] = self._jobs[new], self._jobs[idx]
        self._active_job_id = job_id
        self._refresh_queue_view()
        self.queue_panel.select_job(job_id)
        self._schedule_save()

    def _duplicate_jobs(self, job_ids: object) -> None:
        if self._is_rendering:
            return
        ids = [j for j in (job_ids or []) if isinstance(j, int)]
        if not ids:
            return
        last_new_id: Optional[int] = None
        # Walk a copy of the current order; insert each clone right after its source.
        for source_id in ids:
            idx = next((i for i, j in enumerate(self._jobs) if j.id == source_id), -1)
            if idx < 0:
                continue
            src = self._jobs[idx]
            clone = dataclasses.replace(
                src,
                id=self._next_job_id,
                status="idle",
                progress=0.0,
                error="",
                attempts=0,
                material_assignments=[
                    MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode)
                    for a in src.material_assignments
                ],
                render_options=(dataclasses.replace(src.render_options) if src.render_options else None),
            )
            clone.label = f"{src.label} copy"
            self._next_job_id += 1
            last_new_id = clone.id
            self._jobs.insert(idx + 1, clone)
        if last_new_id is not None:
            self._active_job_id = last_new_id
        self._refresh_job_outputs()
        self._refresh_queue_view()
        if last_new_id is not None:
            self.queue_panel.select_job(last_new_id)
        self._schedule_save()

    def _refresh_job_outputs(self) -> None:
        batch = len(self._jobs) > 1
        for j in self._jobs:
            src = j.material_assignments[0].video_path if j.material_assignments else j.video_path
            if not src:
                continue
            try:
                # Resolve output_format: prefer job's own render options, fall back to profile combo
                opts = j.render_options
                if opts and opts.output_format:
                    out_fmt = opts.output_format
                else:
                    prof = j.output_profile or self.render_panel.profile_combo.currentText()
                    out_fmt, _ = OUTPUT_PROFILES.get(prof, ("MPEG4", "H264"))
                w = opts.width if opts else 1920
                h = opts.height if opts else 1080
                extra = {
                    "camera": j.target_camera or self.scene_panel.camera_combo.currentText(),
                    "width": w,
                    "height": h,
                    "res": f"{w}x{h}",
                    "fps": opts.fps if opts else self.render_panel.fps_edit.text().strip(),
                }
                j.output_path = resolve_output_path(
                    output_input=j.output_input or self.render_panel.output_edit.text().strip(),
                    scene_path=j.scene_path or self.scene_panel.scene_edit.text().strip(),
                    video_path=src,
                    is_batch=batch,
                    job_label=j.label or Path(src).stem,
                    output_format=out_fmt,
                    extra_tokens=extra,
                    create=False,   # don't make folders while drafting (esp. inside a watch folder)
                )
            except Exception:
                j.output_path = ""

    def _preflight(self) -> list[str]:
        errs: list[str] = []
        blender = self._ensure_blender(interactive=False)
        if not blender:
            errs.append("Blender executable is required.")
        scene = self.scene_panel.scene_edit.text().strip()
        if not scene or not file_exists(scene):
            errs.append("Valid scene file is required.")
        if not self._jobs:
            errs.append("Queue at least one job first.")
        if not self.render_panel.output_edit.text().strip():
            errs.append("Output path is required.")

        try:
            fs = int(self.render_panel.frame_start_edit.text())
            fe = int(self.render_panel.frame_end_edit.text())
            st = int(self.render_panel.frame_step_edit.text())
            if fe < fs:
                errs.append("Frame end must be >= frame start.")
            if st <= 0:
                errs.append("Frame step must be > 0.")
        except ValueError:
            errs.append("Frame range values must be integers.")

        return errs

    @staticmethod
    def _job_has_mapping(job: RenderJob) -> bool:
        """A job is renderable only if a video is connected to a material."""
        return any(a.material_name and a.video_path for a in (job.material_assignments or []))

    def _video_info_cached(self, path: str):
        cache = getattr(self, "_video_info_cache", None)
        if cache is None:
            cache = {}
            self._video_info_cache = cache
        if path not in cache:
            try:
                cache[path] = _parse_mp4_info(path)
            except Exception:
                cache[path] = None
        return cache[path]

    def _frame_range_warnings(self, pending: list[RenderJob]) -> list[str]:
        warns: list[str] = []
        for j in pending:
            src = j.material_assignments[0].video_path if j.material_assignments else j.video_path
            if not src or not file_exists(src):
                continue
            opts = j.render_options
            fe = opts.frame_end if opts else 0
            info = self._video_info_cached(src)
            if info:
                frames, _ = info
                if frames and fe > frames:
                    warns.append(
                        f"“{Path(src).name}”: frame range ends at {fe}, but the video has "
                        f"{frames} frames — the extra frames will freeze on the last frame."
                    )
        return warns

    def _disk_space_warnings(self, pending: list[RenderJob]) -> list[str]:
        warns: list[str] = []
        seen: set[str] = set()
        for j in pending:
            out = (j.output_path or "").strip()
            if not out:
                continue
            d = Path(out).expanduser().parent
            while not d.exists() and d.parent != d:
                d = d.parent
            if not d.exists() or str(d) in seen:
                continue
            seen.add(str(d))
            try:
                free = shutil.disk_usage(str(d)).free
                if free < 2 * 1024 ** 3:  # under 2 GB
                    warns.append(f"Low disk space on “{d}”: {free / 1024 ** 3:.1f} GB free.")
            except Exception:
                pass
        return warns

    @staticmethod
    def _unique_path(path: Path) -> str:
        path = Path(path)
        parent = path.parent
        if path.suffix:
            stem, ext = path.stem, path.suffix
            i = 2
            while (parent / f"{stem}_v{i}{ext}").exists():
                i += 1
            return str(parent / f"{stem}_v{i}{ext}")
        base, i = path.name, 2
        while (parent / f"{base}_v{i}").exists():
            i += 1
        return str(parent / f"{base}_v{i}")

    def _resolve_output_conflicts(self, pending: list[RenderJob]) -> bool:
        """Return True to proceed. Detects existing outputs and lets the user
        Overwrite, Auto-rename (keep both), or Cancel."""
        existing: list[tuple[RenderJob, Path]] = []
        for j in pending:
            p = (j.output_path or "").strip()
            if not p:
                continue
            path = Path(p).expanduser()
            is_seq = (j.output_profile in ("PNG Sequence", "OpenEXR Sequence")) or not path.suffix
            if is_seq:
                if path.is_dir() and any(path.iterdir()):
                    existing.append((j, path))
            elif path.exists():
                existing.append((j, path))
        if not existing:
            return True

        names = "\n".join(f"•  {p.name}" for _, p in existing[:8])
        more = "" if len(existing) <= 8 else f"\n…and {len(existing) - 8} more"
        box = QMessageBox(self)
        box.setWindowTitle("Outputs Already Exist")
        box.setText(f"{len(existing)} output(s) already exist:\n{names}{more}")
        box.setInformativeText("Overwrite them, auto-rename to keep both, or cancel?")
        ow = box.addButton("Overwrite", QMessageBox.DestructiveRole)
        rn = box.addButton("Auto-rename", QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is ow:
            return True
        if clicked is rn:
            for j, p in existing:
                j.output_path = self._unique_path(p)
            self._refresh_queue_view()
            return True
        return False

    def _start_render(self, render_all: bool = False, only_job_ids: Optional[set] = None) -> None:
        if self._is_rendering:
            return

        # Friendly guard: nothing is connected to render yet.
        if not self._jobs or not any(self._job_has_mapping(j) for j in self._jobs):
            QMessageBox.warning(
                self, "Nothing to Render",
                "No video is connected to a material yet.\n\n"
                "Add a video, select it together with a material, then click the "
                "link button to connect them. The connected job appears in the "
                "Queue — then press Start.",
            )
            return

        # Cinema 4D scenes render via c4dpy/Redshift; others via Blender.
        _scene_now = self.scene_panel.scene_edit.text().strip()
        _is_c4d = _scene_now.lower().endswith(".c4d")
        if _is_c4d:
            c4dpy = self._ensure_c4dpy(interactive=True)
            if not c4dpy:
                return
            blender = self._blender_path
        else:
            c4dpy = ""
            blender = self._ensure_blender(interactive=True)
            if not blender:
                return

        errs = self._preflight()
        if errs:
            QMessageBox.critical(self, "Preflight Failed", "\n".join(errs))
            return

        if only_job_ids is not None:
            selected_ids = set(only_job_ids)
        else:
            selected_ids = set(self.queue_panel.selected_job_ids()) if not render_all else set(j.id for j in self._jobs)
        pending = [j for j in self._jobs if j.id in selected_ids and j.status != "success"]
        if not pending:
            QMessageBox.information(self, "Nothing To Do", "No queued jobs selected (or all selected jobs already successful).")
            return

        # Non-blocking warning if the frame range overshoots the source video.
        warnings = self._frame_range_warnings(pending) + self._disk_space_warnings(pending)
        if warnings:
            ans = QMessageBox.warning(
                self, "Frame Range Warning",
                "\n\n".join(warnings) + "\n\nProceed anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if ans != QMessageBox.Yes:
                return

        # Overwrite protection.
        if not self._resolve_output_conflicts(pending):
            return

        try:
            worker = _resolve_runtime_script("blender_worker.py")
            c4d_worker = _resolve_runtime_script("c4d_worker.py") if _is_c4d else ""
        except Exception as exc:
            QMessageBox.critical(self, "Worker Missing", str(exc))
            return
        _ffmpeg = (find_ffmpeg_tool("ffmpeg") or "") if _is_c4d else ""

        # Live preview: one temp JPEG the worker rewrites each frame.
        self._preview_path = ""
        if getattr(self, "_preview_enabled", True):
            self._preview_path = str(Path(tempfile.gettempdir()) / "rmp_live_preview.jpg")
            try:
                Path(self._preview_path).unlink(missing_ok=True)
            except Exception:
                pass

        entries: list[dict] = []
        for j in pending:
            asn = [MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode) for a in j.material_assignments]
            opts = j.render_options or self.render_panel.render_options()
            out_fmt, codec = OUTPUT_PROFILES.get(j.output_profile or "H264 MP4", ("MPEG4", "H264"))
            opts = dataclasses.replace(opts, output_format=out_fmt, codec=codec)

            # Create the output location now (drafting no longer pre-creates it,
            # so a watch folder stays clean until you actually render).
            try:
                op = Path(j.output_path)
                (op if op.suffix == "" else op.parent).mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

            primary = asn[0] if asn else MaterialVideoAssignment("", j.video_path)
            audio_src = asn if asn else ([primary] if primary.video_path else [])
            cfg = JobConfig(
                scene_path=j.scene_path,
                video_path=primary.video_path,
                target_material=primary.material_name,
                target_camera=j.target_camera,
                output_path=j.output_path,
                render=opts,
                safe_mode=j.safe_mode,
                use_deadline=j.use_deadline and self.deadline_panel.use_dl_cb.isChecked(),
                deadline_pool=j.deadline_pool,
                deadline_secondary_pool=j.deadline_secondary_pool,
                deadline_group=j.deadline_group,
                deadline_priority=j.deadline_priority,
                deadline_comment=j.deadline_comment,
                deadline_department=j.deadline_department,
                deadline_chunk_size=j.deadline_chunk_size,
                deadline_suspended=j.deadline_suspended,
                submit_scene=getattr(j, 'deadline_submit_scene', True),
                deadline_job_name_template=j.deadline_job_name_template,
                deadline_machine_limit=j.deadline_machine_limit,
                deadline_limits=j.deadline_limits,
                deadline_command_path=j.deadline_command_path,
                deadline_repo_path=getattr(j, 'deadline_repo_path', ""),
                deadline_whitelist=getattr(j, 'deadline_whitelist', ""),
                preview_path=self._preview_path if not j.use_deadline else "",
                audio_paths=self._audio_paths_for(audio_src),
                material_assignments=asn,
                ffmpeg_path=(_ffmpeg if str(j.scene_path).lower().endswith(".c4d") else ""),
            )
            entries.append({"id": j.id, "label": j.label, "cfg": cfg})

        self._is_rendering = True
        self.queue_panel.render_selected_btn.setEnabled(False)
        self.queue_panel.render_all_btn.setEnabled(False)
        self.queue_panel.queue_btn.setEnabled(False)

        self._job_started = {}
        self._render_t0 = time.monotonic()
        self.queue_panel.set_progress(0, "Starting…")

        if self._preview_path:
            self.preview_panel.clear_preview()
            self.preview_dock.show()
            self.preview_dock.raise_()
            if self._preview_timer is None:
                self._preview_timer = QTimer(self)
                self._preview_timer.timeout.connect(self._poll_preview)
            self._preview_timer.start(400)

        self._render_thread = RenderThread(blender, worker, entries, c4dpy=c4dpy, c4d_worker=c4d_worker)
        self._render_thread.log.connect(self._append_log)
        self._render_thread.job_update.connect(self._on_job_update)
        self._render_thread.job_error.connect(self._on_job_error)
        self._render_thread.all_done.connect(self._on_render_done)
        self._render_thread.start()

    def _sync_preview_frame_range(self) -> None:
        """Mirror the render settings' frame range onto the preview frame
        picker so the slider always spans the renderable frames."""
        def _i(text: str, d: int) -> int:
            try:
                return int(float(text.strip()))
            except (ValueError, AttributeError):
                return d
        rp = self.render_panel
        start = _i(rp.frame_start_edit.text(), 1)
        end = _i(rp.frame_end_edit.text(), start)
        self.preview_panel.set_frame_range(start, end)

    def _preview_ready(self) -> bool:
        """The preview works off the live UI, independent of the render queue. It
        only needs a scene — with no mappings yet it just previews the bare 3D
        model, so you can frame up the scene before linking any video."""
        scene = self.scene_panel.scene_edit.text().strip()
        return bool(scene and file_exists(scene))

    def _export_prepared_blend(self) -> None:
        """Bake the current video mapping + render settings into a standalone
        .blend that any render farm (Deadline, BlendFarm, cloud, plain Blender)
        can render — no Deadline required."""
        if self._is_rendering:
            QMessageBox.information(self, "Busy", "Finish the current render first.")
            return
        scene = self.scene_panel.scene_edit.text().strip()
        assignments = self.scene_panel.get_assignments()
        if not scene or not file_exists(scene) or not assignments:
            QMessageBox.information(self, "Export Prepared .blend",
                                    "Load a scene and map at least one video first.")
            return
        blender = self._ensure_blender(interactive=True)
        if not blender:
            return
        try:
            worker = _resolve_runtime_script("blender_worker.py")
        except Exception as exc:
            QMessageBox.warning(self, "Export Failed", str(exc))
            return
        default_name = f"{Path(scene).stem}_prepared.blend"
        out, _ = QFileDialog.getSaveFileName(
            self, "Export Prepared .blend", str(Path.home() / default_name), "Blender (*.blend)")
        if not out:
            return
        if not out.lower().endswith(".blend"):
            out += ".blend"
        # Offer to pack the videos in so the .blend is fully portable (no shared
        # storage needed) — at the cost of a larger file.
        pack = QMessageBox.question(
            self, "Pack video files?",
            "Pack the video file(s) into the .blend so it renders on any machine "
            "without shared storage?\n\nYes = one self-contained (larger) file.\n"
            "No  = smaller file; workers must be able to reach the source videos.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) == QMessageBox.Yes

        asn = [MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode) for a in assignments]
        opts = self.render_panel.render_options()
        cfg = JobConfig(
            scene_path=scene, video_path=asn[0].video_path,
            target_material=asn[0].material_name,
            target_camera=self.scene_panel.camera_combo.currentText(),
            output_path=str(Path(out).with_suffix("")), render=opts, safe_mode=True,
            material_assignments=asn, prepared_blend_path=out, pack_blend=pack,
        )
        self._append_log(f"[app] Exporting prepared .blend → {out} (pack={pack})")
        self._export_thread = ExportBlendThread(blender, worker, cfg, out)
        self._export_thread.log.connect(self._append_log)
        self._export_thread.done.connect(self._on_export_blend_done)
        self._export_thread.start()

    def _on_export_blend_done(self, ok: bool, info: str) -> None:
        if not ok:
            self._append_log(f"[error] Prepared .blend export failed: {info}")
            QMessageBox.warning(self, "Export Failed", info)
            return
        self._append_log(f"[app] Prepared .blend ready: {info}")
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", info])
            elif os.name == "nt":
                subprocess.Popen(["explorer", "/select,", info])
            else:
                subprocess.Popen(["xdg-open", str(Path(info).parent)])
        except Exception:
            pass

    def _render_preview_frame(self) -> None:
        """Render the selected frame from the *current UI state* into the Live
        Preview pane, without queuing a full render and without needing an
        active queue job."""
        if self._is_rendering:
            return
        if getattr(self, "_preview_thread", None) is not None and self._preview_thread.isRunning():
            # A preview is already rendering — remember that state changed so we
            # re-render with the *latest* settings as soon as it finishes. Without
            # this, rapid changes get dropped and the preview looks stuck.
            self._preview_pending = True
            return
        scene = self.scene_panel.scene_edit.text().strip()
        assignments = self.scene_panel.get_assignments()
        if not scene or not file_exists(scene):
            return  # no scene yet — nothing to preview
        # With no mappings we still preview the bare 3D model.
        self._preview_pending = False
        is_c4d = scene.lower().endswith(".c4d")
        if is_c4d:
            c4dpy = self._ensure_c4dpy(interactive=True)
            if not c4dpy:
                return
            blender = self._blender_path
        else:
            c4dpy = ""
            blender = self._ensure_blender(interactive=True)
            if not blender:
                return
        try:
            worker = _resolve_runtime_script("blender_worker.py")
            c4d_worker = _resolve_runtime_script("c4d_worker.py") if is_c4d else ""
        except Exception as exc:
            self._show_toast(str(exc), "error")
            return
        # Everything below comes straight from the live UI, not a queued job.
        asn = [MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode) for a in assignments]
        camera = self.scene_panel.camera_combo.currentText()
        opts = self.render_panel.render_options()
        # Render the frame the user picked. We keep the FULL frame range on the
        # options (so the worker maps the video over the whole timeline and the
        # clip is in sync), and pass the chosen frame via preview_frame — the
        # worker collapses the render range to that single frame.
        fs = self.preview_panel.current_frame()
        # Preview render resolution = output resolution × the chosen fraction.
        scale = self.preview_panel.preview_scale()
        pct = max(1, min(100, round(scale * 100)))
        opts = dataclasses.replace(opts, output_format="PNG", codec="NONE", resolution_percentage=pct)
        self._preview_started = time.monotonic()
        self._preview_frame_num = fs
        primary_video = asn[0].video_path if asn else ""
        primary_mat = asn[0].material_name if asn else ""
        out_dir = tempfile.mkdtemp(prefix="rmp_previewframe_")
        cfg = JobConfig(
            scene_path=scene, video_path=primary_video,
            target_material=primary_mat, target_camera=camera,
            output_path=out_dir, render=opts, safe_mode=True, material_assignments=asn,
            preview_frame=fs, ffmpeg_path=(find_ffmpeg_tool("ffmpeg") or "") if is_c4d else "",
        )
        self.preview_dock.show()
        self.preview_dock.raise_()
        self.preview_panel.preview_frame_btn.setEnabled(False)
        self.preview_panel.caption.setText(f"Rendering preview · frame {fs}…")
        self._append_log(f"[app] Preview: frame={fs} camera={camera!r} scale={pct}% engine={'C4D/Redshift' if is_c4d else 'Blender'}")
        self._preview_thread = PreviewFrameThread(blender, worker, cfg, out_dir,
                                                  c4dpy=c4dpy, c4d_worker=c4d_worker)
        self._preview_thread.log.connect(self._append_log)
        self._preview_thread.done.connect(self._on_preview_frame_done)
        self._preview_thread.start()

    def _on_preview_frame_done(self, path: str, error: str) -> None:
        self.preview_panel.preview_frame_btn.setEnabled(True)
        if error or not path:
            self._show_toast("Preview failed: " + (error or "no frame"), "error")
            self.preview_panel.caption.setText("Preview failed — see log")
        else:
            self.preview_panel.set_image_path(path)
            fn = getattr(self, "_preview_frame_num", None)
            # Stats: frame · actual pixel size · render time.
            bits = []
            if fn is not None:
                bits.append(f"frame {fn}")
            pm = self.preview_panel._pixmap
            if pm is not None and not pm.isNull():
                bits.append(f"{pm.width()}×{pm.height()}")
            started = getattr(self, "_preview_started", None)
            if started is not None:
                bits.append(f"{time.monotonic() - started:.1f}s")
            self.preview_panel.caption.setText("Preview · " + "  ·  ".join(bits) if bits else "Preview frame")
        # If settings changed while this render was running, re-render with the
        # latest state so the preview always converges to what's on screen.
        if getattr(self, "_preview_pending", False):
            self._preview_pending = False
            QTimer.singleShot(50, self._render_preview_frame)

    def _on_job_error(self, job_id: int, message: str) -> None:
        for j in self._jobs:
            if j.id == job_id:
                j.error = message
                break

    def _on_job_update(self, job_id: int, status: str, progress: float) -> None:
        started = getattr(self, "_job_started", None)
        if started is not None and status == "running" and job_id not in started:
            started[job_id] = time.monotonic()
        for j in self._jobs:
            if j.id == job_id:
                if status == "running" and j.status != "running":
                    j.error = ""  # clear any prior failure on (re)start
                j.status = status
                j.progress = progress
                if status == "success":
                    j.selected = False  # auto-uncheck completed jobs
                if status in {"success", "failed", "cancelled"}:
                    j.attempts += 1
                    start = (started or {}).get(job_id)
                    dur = (time.monotonic() - start) if start else 0.0
                    self._job_durations[job_id] = dur
                    self._record_history(j, status, dur)
                break
        self._refresh_queue_view()
        self._update_progress_caption()

    def _record_history(self, job: RenderJob, status: str, duration: float) -> None:
        opts = job.render_options
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "label": job.label,
            "scene": Path(job.scene_path).name if job.scene_path else "",
            "output": job.output_path,
            "status": status,
            "frames": (f"{opts.frame_start}-{opts.frame_end}" if opts else ""),
            "duration": round(duration, 1),
        }
        try:
            HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            hist = []
            if HISTORY_PATH.exists():
                hist = json.loads(HISTORY_PATH.read_text())
            hist.insert(0, entry)
            HISTORY_PATH.write_text(json.dumps(hist[:200], indent=2))
        except Exception:
            pass

    @staticmethod
    def _fmt_dur(seconds: float) -> str:
        seconds = int(max(0, seconds))
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}h{m:02d}m"
        if m:
            return f"{m}m{s:02d}s"
        return f"{s}s"

    def _update_progress_caption(self) -> None:
        if not hasattr(self, "queue_panel"):
            return
        if not self._is_rendering:
            total = len(self._jobs)
            done = sum(1 for j in self._jobs if j.status == "success")
            if total and done == total:
                self.queue_panel.set_progress(100, f"All {total} done")
            else:
                self.queue_panel.set_progress(0, "Ready")
            return

        sel = [j for j in self._jobs if j.selected] or self._jobs
        total = len(sel)
        if not total:
            return
        completed = sum(1 for j in sel if j.status in ("success", "failed", "cancelled"))
        cur = next((j for j in sel if j.status == "running"), None)
        frac = (cur.progress / 100.0) if cur else 0.0
        overall = (completed + frac) / total * 100.0
        if cur:
            caption = f"Job {min(completed + 1, total)}/{total} · {cur.progress:.0f}%"
            started = getattr(self, "_job_started", {}).get(cur.id)
            if started and cur.progress > 1:
                elapsed = time.monotonic() - started
                eta = elapsed * (100.0 - cur.progress) / cur.progress
                caption += f" · {self._fmt_dur(elapsed)} elapsed · ~{self._fmt_dur(eta)} left"
        else:
            caption = f"{completed}/{total} jobs"
        self.queue_panel.set_progress(overall, caption)

    def _poll_preview(self) -> None:
        if self._preview_path and Path(self._preview_path).exists():
            self.preview_panel.set_image_path(self._preview_path)

    def _on_render_done(self) -> None:
        self._is_rendering = False
        if self._preview_timer is not None:
            self._preview_timer.stop()
        self._poll_preview()  # show the final frame
        self.queue_panel.render_selected_btn.setEnabled(True)
        self.queue_panel.render_all_btn.setEnabled(True)
        self.queue_panel.queue_btn.setEnabled(True)
        done = sum(1 for j in self._jobs if j.status == "success")
        failed = sum(1 for j in self._jobs if j.status == "failed")
        if failed:
            self.queue_panel.set_progress(100, f"{failed} failed")
            self._show_toast(f"Render finished with {failed} failed job(s)", "error")
            self._notify("Render finished with errors", f"{failed} job(s) failed, {done} succeeded")
        else:
            self.queue_panel.set_progress(100, "Complete")
            self._show_toast("All jobs finished successfully", "success")
            self._notify("Render complete", f"All {done} job(s) finished")

        self._write_run_report()

        # Play the freshly rendered video in the preview, if any.
        if self._preview_enabled:
            vid = next(
                (j.output_path for j in reversed(self._jobs)
                 if j.status == "success" and j.output_path
                 and Path(j.output_path).suffix.lower() in VIDEO_EXTENSIONS
                 and Path(j.output_path).exists()),
                "",
            )
            if vid:
                self.preview_panel.play_video(vid)
        self._schedule_save()
        self._run_when_done_action()

    # ── Notifications / when-done / reports ──────────────────────────────
    def _notify(self, title: str, message: str) -> None:
        if sys.platform != "darwin":
            return
        try:
            safe_t = title.replace('"', "'")
            safe_m = message.replace('"', "'")
            subprocess.Popen([
                "osascript", "-e",
                f'display notification "{safe_m}" with title "Render Mapper Pro" subtitle "{safe_t}"',
            ])
        except Exception:
            pass

    def _run_when_done_action(self) -> None:
        if self._when_done == "quit":
            QTimer.singleShot(1800, QApplication.quit)
        elif self._when_done == "sleep":
            try:
                QTimer.singleShot(1800, lambda: subprocess.Popen(
                    ["osascript", "-e", 'tell application "System Events" to sleep']))
            except Exception:
                pass

    def _write_run_report(self) -> None:
        try:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "blender": self._blender_path,
                "scene": self.scene_panel.scene_edit.text().strip(),
                "jobs": [
                    {
                        "label": j.label,
                        "status": j.status,
                        "output": j.output_path,
                        "frames": (f"{j.render_options.frame_start}-{j.render_options.frame_end}"
                                   if j.render_options else ""),
                        "duration_s": round(self._job_durations.get(j.id, 0.0), 1),
                        "error": j.error,
                    }
                    for j in self._jobs
                ],
            }
            path = REPORTS_DIR / f"run_report_{stamp}.json"
            path.write_text(json.dumps(report, indent=2))
            self._last_report_path = str(path)
            if hasattr(self, "_open_report_action"):
                self._open_report_action.setEnabled(True)
        except Exception:
            pass

    def _open_last_report(self) -> None:
        if self._last_report_path and Path(self._last_report_path).exists():
            self._open_path(self._last_report_path)
        else:
            self._show_toast("No run report yet", "warning")

    @staticmethod
    def _open_path(path: str) -> None:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif os.name == "nt":
                os.startfile(path)  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            pass

    # ── Project save/open ────────────────────────────────────────────────
    def _save_project(self) -> None:
        p, _ = QFileDialog.getSaveFileName(
            self, "Save Project", str(Path.home() / f"render_mapper_project{PROJECT_EXT}"),
            f"Render Mapper Project (*{PROJECT_EXT})")
        if not p:
            return
        if not p.lower().endswith(PROJECT_EXT):
            p += PROJECT_EXT
        try:
            Path(p).write_text(json.dumps(self._profile_dict(), indent=2))
            self._show_toast("Project saved", "success")
        except Exception as exc:
            self._show_toast(f"Save failed: {exc}", "error")

    def _open_project(self) -> None:
        p, _ = QFileDialog.getOpenFileName(
            self, "Open Project", str(Path.home()),
            f"Render Mapper Project (*{PROJECT_EXT})")
        if not p:
            return
        try:
            self._apply_profile_data(json.loads(Path(p).read_text()))
            self._schedule_save()
            self._show_toast("Project loaded", "success")
            scene = self.scene_panel.scene_edit.text().strip()
            if scene and file_exists(scene):
                self._scan_scene()  # auto-scan the project's scene
        except Exception as exc:
            self._show_toast(f"Open failed: {exc}", "error")

    # ── Render history ───────────────────────────────────────────────────
    def _show_history_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Render History")
        dlg.setMinimumSize(660, 420)
        lay = QVBoxLayout(dlg)
        table = QTableWidget(0, 5)
        table.setHorizontalHeaderLabels(["When", "Job", "Status", "Duration", "Output"])
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.horizontalHeader().setStretchLastSection(True)
        hist = []
        try:
            if HISTORY_PATH.exists():
                hist = json.loads(HISTORY_PATH.read_text())
        except Exception:
            hist = []
        for e in hist:
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QTableWidgetItem(str(e.get("time", "")).replace("T", "  ")))
            table.setItem(r, 1, QTableWidgetItem(e.get("label", "")))
            table.setItem(r, 2, QTableWidgetItem(e.get("status", "")))
            table.setItem(r, 3, QTableWidgetItem(self._fmt_dur(float(e.get("duration", 0) or 0))))
            oi = QTableWidgetItem(e.get("output", ""))
            oi.setData(Qt.UserRole, e.get("output", ""))
            table.setItem(r, 4, oi)
        table.setColumnWidth(0, 160)
        table.setColumnWidth(1, 190)
        table.setColumnWidth(2, 70)
        table.setColumnWidth(3, 80)
        lay.addWidget(table)

        def sel_out() -> str:
            r = table.currentRow()
            it = table.item(r, 4) if r >= 0 else None
            return it.data(Qt.UserRole) if it else ""

        row = QHBoxLayout()
        reveal_b = QPushButton("Reveal Output")
        open_b = QPushButton("Open Output")
        clear_b = QPushButton("Clear History")
        reveal_b.clicked.connect(lambda: (sel_out() and Path(sel_out()).exists()
                                          and reveal_in_file_manager(sel_out())))
        open_b.clicked.connect(lambda: sel_out() and self._open_path(sel_out()))

        def do_clear() -> None:
            try:
                HISTORY_PATH.write_text("[]")
            except Exception:
                pass
            table.setRowCount(0)

        clear_b.clicked.connect(do_clear)
        row.addWidget(reveal_b)
        row.addWidget(open_b)
        row.addStretch()
        row.addWidget(clear_b)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def _cancel_render(self) -> None:
        if self._render_thread and self._is_rendering:
            self._render_thread.request_cancel()
            self._append_log("[app] Cancel requested")

    def _skip_render(self) -> None:
        if self._render_thread and self._is_rendering:
            self._render_thread.request_skip()
            self._append_log("[app] Skip current job requested")

    def _copy_diagnostics(self) -> None:
        lines = [
            f"timestamp: {datetime.now().isoformat(timespec='seconds')}",
            f"blender: {self._blender_path}",
            f"scene: {self.scene_panel.scene_edit.text()}",
            f"camera: {self.scene_panel.camera_combo.currentText()}",
            f"videos: {len(self.scene_panel.get_videos())}",
            f"assignments: {len(self.scene_panel.get_assignments())}",
            f"jobs: {len(self._jobs)}",
            f"profile: {self.render_panel.profile_combo.currentText()}",
            f"engine: {self.render_panel.engine_combo.currentText()}",
            f"range: {self.render_panel.frame_start_edit.text()}-{self.render_panel.frame_end_edit.text()}",
        ]
        QApplication.clipboard().setText("\n".join(lines))

    def _test_deadline_connection(self) -> None:
        deadline_cmd = self.deadline_panel.dl_cmd_edit.text().strip()
        if not deadline_cmd:
            deadline_cmd = find_deadlinecommand() or "deadlinecommand"

        self._append_log(f"[deadline] Testing connection using: {deadline_cmd}")
        self.deadline_panel.connection_status_lbl.setText("Connection status: Testing...")
        self.deadline_panel.connection_status_lbl.setStyleSheet(f"color: {self._palette.warning}; font-size: 10px;")
        QApplication.processEvents()

        if not Path(deadline_cmd).exists() and not shutil.which(deadline_cmd):
            self._append_log("[deadline] ERROR: deadlinecommand executable not found.")
            self.deadline_panel.connection_status_lbl.setText("Connection status: deadlinecommand not found")
            self.deadline_panel.connection_status_lbl.setStyleSheet(f"color: {self._palette.danger}; font-size: 10px;")
            QMessageBox.critical(self, "Deadline Connection Error", f"deadlinecommand not found at {deadline_cmd}.\nPlease check your Thinkbox Deadline installation.")
            return

        repo_path = self.deadline_panel.dl_repo_edit.text().strip()
        if repo_path:
            p_args = [deadline_cmd, "RunCommandForRepository", "Direct", repo_path, "-pools"]
            g_args = [deadline_cmd, "RunCommandForRepository", "Direct", repo_path, "-groups"]
            m_args = [deadline_cmd, "RunCommandForRepository", "Direct", repo_path, "-GetSlaveNames"]
        else:
            p_args = [deadline_cmd, "-pools"]
            g_args = [deadline_cmd, "-groups"]
            m_args = [deadline_cmd, "-GetSlaveNames"]

        try:
            # Query pools
            p_res = subprocess.run(p_args, capture_output=True, text=True, timeout=8)
            # Query groups
            g_res = subprocess.run(g_args, capture_output=True, text=True, timeout=8)
            # Query machines
            m_res = subprocess.run(m_args, capture_output=True, text=True, timeout=8)
            
            if p_res.returncode == 0:
                pools = [line.strip() for line in p_res.stdout.splitlines() if line.strip()]
                current_pool = self.deadline_panel.dl_pool_combo.currentText()
                current_sec_pool = self.deadline_panel.dl_sec_pool_combo.currentText()
                self.deadline_panel.dl_pool_combo.clear()
                self.deadline_panel.dl_sec_pool_combo.clear()
                self.deadline_panel.dl_pool_combo.addItems(pools)
                self.deadline_panel.dl_sec_pool_combo.addItems([""] + pools)
                if current_pool:
                    self.deadline_panel.dl_pool_combo.setCurrentText(current_pool)
                if current_sec_pool:
                    self.deadline_panel.dl_sec_pool_combo.setCurrentText(current_sec_pool)
                self._append_log(f"[deadline] Successfully queried {len(pools)} pools from repository.")
            
            if g_res.returncode == 0:
                groups = [line.strip() for line in g_res.stdout.splitlines() if line.strip()]
                current_group = self.deadline_panel.dl_group_combo.currentText()
                self.deadline_panel.dl_group_combo.clear()
                self.deadline_panel.dl_group_combo.addItems([""] + groups)
                if current_group:
                    self.deadline_panel.dl_group_combo.setCurrentText(current_group)
                self._append_log(f"[deadline] Successfully queried {len(groups)} groups from repository.")

            if m_res.returncode == 0:
                machines = [line.strip() for line in m_res.stdout.splitlines() if line.strip()]
                self.deadline_panel.dl_machines_list.blockSignals(True)
                currently_checked = set()
                for i in range(self.deadline_panel.dl_machines_list.count()):
                    item = self.deadline_panel.dl_machines_list.item(i)
                    if item.checkState() == Qt.Checked:
                        currently_checked.add(item.text().strip())
                
                self.deadline_panel.dl_machines_list.clear()
                for m in machines:
                    item = QListWidgetItem(m)
                    item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                    if m in currently_checked or not currently_checked:
                        # Default to checked if nothing was checked before (meaning "use all")
                        # Or if it was explicitly checked previously.
                        item.setCheckState(Qt.Checked)
                    else:
                        item.setCheckState(Qt.Unchecked)
                    self.deadline_panel.dl_machines_list.addItem(item)
                self.deadline_panel.dl_machines_list.blockSignals(False)
                self._append_log(f"[deadline] Successfully queried {len(machines)} machines from repository.")
            
            if p_res.returncode == 0:
                self.deadline_panel.connection_status_lbl.setText("Connection status: Connected")
                self.deadline_panel.connection_status_lbl.setStyleSheet(f"color: {self._palette.success}; font-size: 10px;")
                QMessageBox.information(self, "Deadline Connection", "Successfully connected to Deadline repository and updated pools, groups, and machine list!")
            else:
                self.deadline_panel.connection_status_lbl.setText("Connection status: Connection failed")
                self.deadline_panel.connection_status_lbl.setStyleSheet(f"color: {self._palette.danger}; font-size: 10px;")
                QMessageBox.warning(self, "Deadline Warning", f"deadlinecommand returned exit code {p_res.returncode}.\nStderr: {p_res.stderr.strip()}")
        except Exception as exc:
            self._append_log(f"[deadline] ERROR connection test failed: {exc}")
            self.deadline_panel.connection_status_lbl.setText(f"Connection status: Error ({type(exc).__name__})")
            self.deadline_panel.connection_status_lbl.setStyleSheet(f"color: {self._palette.danger}; font-size: 10px;")
            QMessageBox.critical(self, "Deadline Connection Error", f"Failed to communicate with Deadline:\n{exc}")

    def _export_deadline_files(self) -> None:
        if not self._jobs:
            QMessageBox.warning(self, "No Jobs", "There are no jobs in the queue to export.")
            return

        job = None
        if self._active_job_id is not None:
            job = next((j for j in self._jobs if j.id == self._active_job_id), None)
        if not job:
            job = self._jobs[0]

        dest_dir = QFileDialog.getExistingDirectory(self, "Select Export Directory")
        if not dest_dir:
            return
        
        dest_path = Path(dest_dir)
        job_info_path = dest_path / f"deadline_job_{job.id}.job"
        plugin_info_path = dest_path / f"deadline_plugin_{job.id}.job"

        try:
            asn = [MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode) for a in job.material_assignments]
            opts = job.render_options or self.render_panel.render_options()
            out_fmt, codec = OUTPUT_PROFILES.get(job.output_profile or "H264 MP4", ("MPEG4", "H264"))
            opts = dataclasses.replace(opts, output_format=out_fmt, codec=codec)
            primary = asn[0] if asn else MaterialVideoAssignment("", job.video_path)

            cfg = JobConfig(
                scene_path=job.scene_path,
                video_path=primary.video_path,
                target_material=primary.material_name,
                target_camera=job.target_camera,
                output_path=job.output_path,
                render=opts,
                safe_mode=job.safe_mode,
                use_deadline=True,
                deadline_pool=self.deadline_panel.dl_pool_combo.currentText().strip(),
                deadline_secondary_pool=self.deadline_panel.dl_sec_pool_combo.currentText().strip(),
                deadline_group=self.deadline_panel.dl_group_combo.currentText().strip(),
                deadline_priority=self.deadline_panel.dl_prio_spin.value(),
                deadline_comment=self.deadline_panel.dl_comment_edit.text().strip(),
                deadline_department=self.deadline_panel.dl_dept_edit.text().strip(),
                deadline_chunk_size=self.deadline_panel.dl_chunk_spin.value(),
                deadline_suspended=self.deadline_panel.dl_suspended_cb.isChecked(),
                submit_scene=self.deadline_panel.dl_submit_scene_cb.isChecked(),
                deadline_job_name_template=self.deadline_panel.dl_name_template_edit.text().strip(),
                deadline_machine_limit=self.deadline_panel.dl_machine_limit_spin.value(),
                deadline_limits=self.deadline_panel.dl_limits_edit.text().strip(),
                deadline_command_path=self.deadline_panel.dl_cmd_edit.text().strip(),
                deadline_repo_path=self.deadline_panel.dl_repo_edit.text().strip(),
                deadline_whitelist=self.deadline_panel.get_selected_machines(),
                material_assignments=asn,
            )

            scene_path = Path(cfg.scene_path).expanduser().resolve()
            blender_path = os.path.expanduser(self._blender_path)
            worker = _resolve_runtime_script("blender_worker.py")
            worker_path = Path(worker).expanduser().resolve()

            with open(job_info_path, "w") as f:
                name_template = cfg.deadline_job_name_template or ""
                if name_template:
                    try:
                        name = name_template.format(scene_name=scene_path.name, video_name=Path(cfg.video_path).name if cfg.video_path else "")
                    except Exception:
                        name = f"BlenderRender Job - {scene_path.name}"
                else:
                    name = f"BlenderRender Job - {scene_path.name}"
                f.write(f"Name={name}\n")
                f.write("Plugin=CommandLine\n")
                f.write(f"Frames={cfg.render.frame_start}-{cfg.render.frame_end}\n")
                f.write(f"Priority={cfg.deadline_priority}\n")
                if cfg.deadline_pool:
                    f.write(f"Pool={cfg.deadline_pool}\n")
                if cfg.deadline_secondary_pool:
                    f.write(f"SecondaryPool={cfg.deadline_secondary_pool}\n")
                if cfg.deadline_group:
                    f.write(f"Group={cfg.deadline_group}\n")
                if cfg.deadline_comment:
                    f.write(f"Comment={cfg.deadline_comment}\n")
                if cfg.deadline_department:
                    f.write(f"Department={cfg.deadline_department}\n")
                from core.utils import ext_for_format
                ext = ext_for_format(cfg.render.output_format)
                is_video = ext != ""
                
                chunk_size = cfg.deadline_chunk_size
                if is_video:
                    total_frames = max(1, cfg.render.frame_end - cfg.render.frame_start + 1)
                    chunk_size = total_frames
                    
                if chunk_size > 1:
                    f.write(f"ChunkSize={chunk_size}\n")
                if cfg.deadline_suspended:
                    f.write("InitialStatus=Suspended\n")
                if cfg.deadline_machine_limit > 0:
                    f.write(f"MachineLimit={cfg.deadline_machine_limit}\n")
                if cfg.deadline_limits:
                    f.write(f"Limits={cfg.deadline_limits}\n")
                whitelist = getattr(cfg, 'deadline_whitelist', "").strip()
                if whitelist:
                    f.write(f"Whitelist={whitelist}\n")

                from core.utils import ext_for_format
                if cfg.output_path:
                    out_path = Path(cfg.output_path)
                    if out_path.suffix:
                        f.write(f"OutputDirectory0={out_path.parent}\n")
                        f.write(f"OutputFilename0={out_path.name}\n")
                    else:
                        f.write(f"OutputDirectory0={out_path}\n")
                        ext = ext_for_format(cfg.render.output_format) or ".png"
                        f.write(f"OutputFilename0=####{ext}\n")

            scene_arg = scene_path.name if cfg.submit_scene else str(scene_path)
            worker_arg = worker_path.name
            with open(plugin_info_path, "w") as f:
                f.write(f"Executable={blender_path}\n")
                f.write(f'Arguments=-b "{scene_arg}" --python "{worker_arg}" -- "<CONFIG_PATH_OVERRIDE>"\n')

            QMessageBox.information(
                self, "Export Successful",
                f"Successfully exported Deadline job files to:\n- {job_info_path.name}\n- {plugin_info_path.name}\n\nIn the directory: {dest_dir}"
            )
            self._append_log(f"[deadline] Exported job files to {dest_dir}")
        except Exception as exc:
            QMessageBox.critical(self, "Export Error", f"Failed to export files:\n{exc}")
            self._append_log(f"[deadline] ERROR exporting files: {exc}")

    def _save_preset(self) -> None:
        name, ok = QInputDialog.getText(self, "Save Preset", "Preset name:")
        if not ok or not name.strip():
            return
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("._")
        if not safe:
            QMessageBox.warning(self, "Invalid", "Preset name is invalid.")
            return
        try:
            PRESETS_DIR.mkdir(parents=True, exist_ok=True)
            p = PRESETS_DIR / f"{safe}{PRESET_EXT}"
            # A preset is a reusable render recipe (settings only) — not the
            # scene/clips/queue. Use Profile → Save Project for the full setup.
            p.write_text(json.dumps(self.render_panel.settings_dict(), indent=2))
            self._refresh_preset_browser()
            self._show_toast(f"Preset “{safe}” saved", "success")
        except Exception as exc:
            QMessageBox.warning(self, "Save Failed", str(exc))

    def _load_preset(self) -> None:
        try:
            PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        p, _ = QFileDialog.getOpenFileName(
            self, "Load Preset", str(PRESETS_DIR),
            f"Render Mapper Preset (*{PRESET_EXT})")
        if not p:
            return
        self._load_preset_path(p)

    def _load_preset_entry(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        p = str(entry.get("path", "")).strip()
        if p:
            self._load_preset_path(p)

    def _load_preset_path(self, preset_path: str) -> None:
        try:
            d = json.loads(Path(preset_path).read_text())
            self.render_panel.apply_settings(d)  # settings only — keeps current scene/clips
            self._schedule_save()
            self._show_toast(f"Applied preset “{Path(preset_path).stem}”", "success")
        except Exception as exc:
            QMessageBox.warning(self, "Load Failed", str(exc))

    def _delete_preset_entry(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        p = str(entry.get("path", "")).strip()
        if p:
            self._delete_preset_path(p)

    def _apply_preset_to_queue(self, entry: object, checked_only: bool) -> None:
        if not isinstance(entry, dict):
            return

        target_ids = set(self.queue_panel.selected_job_ids() if checked_only else self.queue_panel.selected_row_job_ids())
        if not target_ids:
            QMessageBox.information(self, "Preset", "Select queue rows (or check Run) before applying a preset.")
            return

        p = str(entry.get("path", "")).strip()
        if not p:
            return
        preset_dict: Optional[dict] = None
        try:
            preset_dict = json.loads(Path(p).read_text())
        except Exception as exc:
            QMessageBox.warning(self, "Preset", f"Failed to read preset: {exc}")
            return

        def coerce(field: str, value, fallback):
            try:
                if isinstance(fallback, bool):
                    return bool(value)
                if isinstance(fallback, int):
                    return int(str(value))
                if isinstance(fallback, float):
                    return float(str(value))
                return str(value)
            except Exception:
                return fallback

        ro_fields = RenderOptions.__dataclass_fields__
        for j in self._jobs:
            if j.id not in target_ids:
                continue
            opts = j.render_options or self.render_panel.render_options()
            # Apply every render-recipe field present in the preset (robust).
            kwargs = {}
            for k in ro_fields:
                if k in preset_dict:
                    kwargs[k] = coerce(k, preset_dict[k], getattr(opts, k))
            j.render_options = dataclasses.replace(opts, **kwargs)
            if "output_profile" in preset_dict and str(preset_dict["output_profile"]).strip():
                j.output_profile = str(preset_dict["output_profile"]).strip()

        if self._active_job_id is not None:
            self._on_queue_job_selected(self._active_job_id)
        self._refresh_job_outputs()
        self._refresh_queue_view()
        self._schedule_save()

    def _delete_preset_path(self, preset_path: str) -> None:
        p = Path(preset_path)
        if not p.exists():
            self._refresh_preset_browser()
            return
        ans = QMessageBox.question(self, "Delete Preset", f"Delete preset '{p.stem}'?")
        if ans != QMessageBox.Yes:
            return
        try:
            p.unlink()
            self._refresh_preset_browser()
        except Exception as exc:
            QMessageBox.warning(self, "Delete Failed", str(exc))

    def _refresh_preset_browser(self) -> None:
        try:
            PRESETS_DIR.mkdir(parents=True, exist_ok=True)
            presets = sorted(PRESETS_DIR.glob(f"*{PRESET_EXT}"), key=lambda x: x.stem.lower())
        except Exception:
            presets = []
        self.presets_panel.set_presets(presets)

    def _open_presets_folder(self) -> None:
        PRESETS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", str(PRESETS_DIR)])
            elif os.name == "nt":
                os.startfile(str(PRESETS_DIR))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(PRESETS_DIR)])
        except Exception:
            pass

    def _profile_dict(self) -> dict:
        videos = self.scene_panel.get_videos()
        layout_state = bytes(self.saveState().toBase64()).decode("ascii")
        layout_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")

        def _opts_dict(opts: Optional[RenderOptions]) -> Optional[dict]:
            if opts is None:
                return None
            return dataclasses.asdict(opts)

        jobs_data = [
            {
                "id": j.id,
                "label": j.label,
                "custom_label": j.custom_label,
                "video_path": j.video_path,
                "output_path": j.output_path,
                "output_input": j.output_input,
                "scene_path": j.scene_path,
                "target_camera": j.target_camera,
                "output_profile": j.output_profile,
                "render_options": _opts_dict(j.render_options),
                "safe_mode": j.safe_mode,
                "status": j.status,
                "progress": j.progress,
                "selected": j.selected,
                "use_deadline": j.use_deadline,
                "deadline_pool": j.deadline_pool,
                "deadline_secondary_pool": j.deadline_secondary_pool,
                "deadline_group": j.deadline_group,
                "deadline_priority": j.deadline_priority,
                "deadline_comment": j.deadline_comment,
                "deadline_department": j.deadline_department,
                "deadline_chunk_size": j.deadline_chunk_size,
                "deadline_suspended": j.deadline_suspended,
                "deadline_submit_scene": getattr(j, 'deadline_submit_scene', True),
                "deadline_job_name_template": j.deadline_job_name_template,
                "deadline_machine_limit": j.deadline_machine_limit,
                "deadline_limits": j.deadline_limits,
                "deadline_command_path": j.deadline_command_path,
                "deadline_repo_path": j.deadline_repo_path,
                "deadline_whitelist": j.deadline_whitelist,
                "material_assignments": [
                    {
                        "material_name": a.material_name,
                        "video_path": a.video_path,
                        "video_name": Path(a.video_path).name,
                        "mapping_mode": a.mapping_mode,
                    }
                    for a in j.material_assignments
                ],
            }
            for j in self._jobs
        ]

        watch_folder, watch_enabled = self.scene_panel.get_watch_folder()
        watch_interval_ms, watch_settle = self.scene_panel.get_watch_options()
        return {
            "version": PROFILE_VERSION,
            "theme_mode": self._theme_mode,
            "accent": self._accent,
            "live_preview": self._preview_enabled,
            "watch_folder": watch_folder,
            "watch_enabled": watch_enabled,
            "watch_interval_ms": watch_interval_ms,
            "watch_settle": watch_settle,
            "autorender_enabled": self._autorender_enabled,
            "autorender_start": self._autorender_start,
            "autorender_output": self._autorender_output,
            "autorender_pattern": self._autorender_pattern,
            "custom_layout": self._custom_layout_state,
            "recent_scenes": self._recent_scenes,
            "when_done": self._when_done,
            "blender_path": self._blender_path,
            "c4dpy_path": self._c4dpy_path,
            "scene_path": self.scene_panel.scene_edit.text().strip(),
            "camera": self.scene_panel.camera_combo.currentText(),
            "width": self.render_panel.width_edit.text().strip(),
            "height": self.render_panel.height_edit.text().strip(),
            "fps": self.render_panel.fps_edit.text().strip(),
            "frame_start": self.render_panel.frame_start_edit.text().strip(),
            "frame_end": self.render_panel.frame_end_edit.text().strip(),
            "frame_step": self.render_panel.frame_step_edit.text().strip(),
            "output_path": self.render_panel.output_edit.text().strip(),
            "engine": self.render_panel.engine_combo.currentText(),
            "output_profile": self.render_panel.profile_combo.currentText(),
            "safe_mode": True,
            "layout_state": layout_state,
            "layout_geometry": layout_geometry,
            "queue_jobs": jobs_data,
            "active_job_id": self._active_job_id,
            "next_job_id": self._next_job_id,
            "video_files": [
                {
                    "path": v,
                    "name": Path(v).name,
                }
                for v in videos
            ],
            "muted_videos": self.scene_panel.get_muted_videos(),
            "material_assignments": [
                {
                    "material_name": a.material_name,
                    "video_path": a.video_path,
                    "video_name": Path(a.video_path).name,
                    "mapping_mode": a.mapping_mode,
                }
                for a in self.scene_panel.get_assignments()
            ],
            "use_deadline": self.deadline_panel.use_dl_cb.isChecked(),
            "deadline_pool": self.deadline_panel.dl_pool_combo.currentText().strip(),
            "deadline_secondary_pool": self.deadline_panel.dl_sec_pool_combo.currentText().strip(),
            "deadline_group": self.deadline_panel.dl_group_combo.currentText().strip(),
            "deadline_priority": self.deadline_panel.dl_prio_spin.value(),
            "deadline_comment": self._deadline_comment,
            "deadline_department": self.deadline_panel.dl_dept_edit.text().strip(),
            "deadline_chunk_size": self.deadline_panel.dl_chunk_spin.value(),
            "deadline_suspended": self.deadline_panel.dl_suspended_cb.isChecked(),
            "deadline_submit_scene": self.deadline_panel.dl_submit_scene_cb.isChecked(),
            "deadline_job_name_template": self._deadline_job_name_template,
            "deadline_machine_limit": self.deadline_panel.dl_machine_limit_spin.value(),
            "deadline_limits": self.deadline_panel.dl_limits_edit.text().strip(),
            "deadline_command_path": self._deadline_command_path,
            "deadline_repo_path": self._deadline_repo_path,
            "deadline_whitelist": self.deadline_panel.get_selected_machines(),
            "deadline_available_pools": [self.deadline_panel.dl_pool_combo.itemText(i) for i in range(self.deadline_panel.dl_pool_combo.count())],
            "deadline_available_groups": [self.deadline_panel.dl_group_combo.itemText(i) for i in range(self.deadline_panel.dl_group_combo.count())],
            "deadline_available_machines": [self.deadline_panel.dl_machines_list.item(i).text().strip() for i in range(self.deadline_panel.dl_machines_list.count())],
        }

    def _apply_profile_data(self, d: dict) -> None:
        # Theme is fixed (dark + orange); ignore any saved theme/accent keys.
        if "live_preview" in d:
            self._preview_enabled = bool(d.get("live_preview", True))
            if hasattr(self, "preview_action"):
                self.preview_action.setChecked(self._preview_enabled)
        if d.get("custom_layout"):
            self._custom_layout_state = str(d.get("custom_layout", ""))
            if hasattr(self, "restore_layout_action"):
                self.restore_layout_action.setEnabled(True)
        recent = d.get("recent_scenes", [])
        if isinstance(recent, list):
            self._recent_scenes = [str(s) for s in recent if s]
            self.scene_panel.set_recent_scenes(self._recent_scenes)
        wd = str(d.get("when_done", self._when_done))
        if wd in ("nothing", "quit", "sleep"):
            self._when_done = wd
            label = {"nothing": "Do Nothing", "quit": "Quit App", "sleep": "Sleep Computer"}[wd]
            if hasattr(self, "_when_actions") and label in self._when_actions:
                self._when_actions[label].setChecked(True)

        self._blender_path = d.get("blender_path", "")
        self._c4dpy_path = d.get("c4dpy_path", "") or self._c4dpy_path   # keep auto-detected if unset
        self.scene_panel.scene_edit.setText(d.get("scene_path", ""))

        pools = d.get("deadline_available_pools", [])
        if pools:
            self.deadline_panel.dl_pool_combo.clear()
            self.deadline_panel.dl_pool_combo.addItems(pools)
            self.deadline_panel.dl_sec_pool_combo.clear()
            self.deadline_panel.dl_sec_pool_combo.addItems([""] + pools)

        groups = d.get("deadline_available_groups", [])
        if groups:
            self.deadline_panel.dl_group_combo.clear()
            self.deadline_panel.dl_group_combo.addItems(groups)

        machines = d.get("deadline_available_machines", [])
        if machines:
            self.deadline_panel.dl_machines_list.blockSignals(True)
            self.deadline_panel.dl_machines_list.clear()
            for m in machines:
                item = QListWidgetItem(m)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(Qt.Unchecked)
                self.deadline_panel.dl_machines_list.addItem(item)
            self.deadline_panel.dl_machines_list.blockSignals(False)

        self.deadline_panel.use_dl_cb.setChecked(bool(d.get("use_deadline", False)))
        self.deadline_panel.dl_pool_combo.setCurrentText(str(d.get("deadline_pool", "")))
        self.deadline_panel.dl_sec_pool_combo.setCurrentText(str(d.get("deadline_secondary_pool", "")))
        self.deadline_panel.dl_group_combo.setCurrentText(str(d.get("deadline_group", "")))
        self.deadline_panel.dl_prio_spin.setValue(int(d.get("deadline_priority", 50)))
        self._deadline_comment = str(d.get("deadline_comment", ""))
        self.deadline_panel.dl_comment_edit.setText(self._deadline_comment)
        self.deadline_panel.dl_dept_edit.setText(str(d.get("deadline_department", "")))
        self.deadline_panel.dl_chunk_spin.setValue(int(d.get("deadline_chunk_size", 1)))
        self.deadline_panel.dl_suspended_cb.setChecked(bool(d.get("deadline_suspended", False)))
        self.deadline_panel.dl_submit_scene_cb.setChecked(bool(d.get("deadline_submit_scene", True)))
        self._deadline_job_name_template = str(d.get("deadline_job_name_template", "BlenderRender Job - {scene_name}"))
        self.deadline_panel.dl_name_template_edit.setText(self._deadline_job_name_template)
        self.deadline_panel.dl_machine_limit_spin.setValue(int(d.get("deadline_machine_limit", 0)))
        self.deadline_panel.dl_limits_edit.setText(str(d.get("deadline_limits", "")))
        self._deadline_command_path = str(d.get("deadline_command_path", ""))
        self._deadline_repo_path = str(d.get("deadline_repo_path", ""))
        self.deadline_panel.dl_cmd_edit.setText(self._deadline_command_path)
        self.deadline_panel.dl_repo_edit.setText(self._deadline_repo_path)
        self.deadline_panel.set_selected_machines(str(d.get("deadline_whitelist", "")))

        self.render_panel.width_edit.setText(str(d.get("width", "1920")))
        self.render_panel.height_edit.setText(str(d.get("height", "1080")))
        self.render_panel.fps_edit.setText(str(d.get("fps", "30")))
        self.render_panel.frame_start_edit.setText(str(d.get("frame_start", "1")))
        self.render_panel.frame_end_edit.setText(str(d.get("frame_end", "250")))
        self.render_panel.frame_step_edit.setText(str(d.get("frame_step", "1")))
        self.render_panel.output_edit.setText(d.get("output_path", ""))

        eng = d.get("engine", "CYCLES")
        idx = self.render_panel.engine_combo.findText(eng)
        if idx >= 0:
            self.render_panel.engine_combo.setCurrentIndex(idx)
        prof = d.get("output_profile", "H264 MP4")
        idx = self.render_panel.profile_combo.findText(prof)
        if idx >= 0:
            self.render_panel.profile_combo.setCurrentIndex(idx)

        raw_videos = d.get("video_files", [])
        video_entries: list[tuple[str, str]] = []
        for item in raw_videos:
            if isinstance(item, str):
                video_entries.append((item, Path(item).name))
            elif isinstance(item, dict):
                vp = str(item.get("path", "")).strip()
                vn = str(item.get("name", "")).strip() or Path(vp).name
                if vp or vn:
                    video_entries.append((vp, vn))

        # Backward compatibility for older presets that only had string paths.
        if not video_entries:
            for item in raw_videos:
                if isinstance(item, str):
                    video_entries.append((item, Path(item).name))

        name_lookup: dict[str, str] = {}
        for v in self.scene_panel.get_videos():
            if file_exists(v):
                name_lookup.setdefault(Path(v).name.lower(), v)
        for vp, vn in video_entries:
            if vp and file_exists(vp):
                name_lookup.setdefault((vn or Path(vp).name).lower(), vp)

        scene_dir = Path(self.scene_panel.scene_edit.text().strip()).expanduser().parent

        def resolve_video(vp: str, vn: str) -> str:
            if vp and file_exists(vp):
                return vp
            key = (vn or Path(vp).name).strip().lower()
            if not key:
                return ""
            matched = name_lookup.get(key)
            if matched and file_exists(matched):
                return matched
            if scene_dir.exists() and scene_dir.is_dir():
                try:
                    for hit in scene_dir.rglob(vn or Path(vp).name):
                        if hit.is_file():
                            found = str(hit)
                            name_lookup[key] = found
                            return found
                except Exception:
                    pass
            return ""

        vids: list[str] = []
        for vp, vn in video_entries:
            resolved = resolve_video(vp, vn)
            if resolved and resolved not in vids:
                vids.append(resolved)

        asn: list[MaterialVideoAssignment] = []
        for item in d.get("material_assignments", []):
            if not isinstance(item, dict):
                continue
            mn = str(item.get("material_name", "")).strip()
            vp = str(item.get("video_path", "")).strip()
            vn = str(item.get("video_name", "")).strip()
            mm = str(item.get("mapping_mode", VIDEO_MAPPING_MODE_EMISSION)).strip().upper()
            resolved = resolve_video(vp, vn)
            if mn and resolved:
                asn.append(MaterialVideoAssignment(mn, resolved, mm))
                if resolved not in vids:
                    vids.append(resolved)

        self.scene_panel.set_videos(vids)
        self.scene_panel.set_assignments(asn)

        muted: list[str] = []
        for p in d.get("muted_videos", []):
            r = resolve_video(str(p), Path(str(p)).name)
            if r:
                muted.append(r)
        self.scene_panel.set_muted_videos(muted)

        # Restore the watch folder (after videos, so the first scan reconciles
        # against the loaded clips and version-updates them if needed).
        try:
            self.scene_panel.set_watch_options(
                int(d.get("watch_interval_ms", 3000)), float(d.get("watch_settle", 2.0)))
        except (TypeError, ValueError):
            pass
        self._autorender_enabled = bool(d.get("autorender_enabled", False))
        self._autorender_start = bool(d.get("autorender_start", False))
        self._autorender_output = str(d.get("autorender_output", "") or "")
        self._autorender_pattern = str(d.get("autorender_pattern", "") or "{clip}_PREVIZ")
        wf = str(d.get("watch_folder", "") or "")
        if wf:
            self.scene_panel.set_watch_folder(wf, bool(d.get("watch_enabled", False)))

        cam = d.get("camera", "")
        if cam:
            idx = self.scene_panel.camera_combo.findText(cam)
            if idx >= 0:
                self.scene_panel.camera_combo.setCurrentIndex(idx)

        self._known_videos = set(vids)

        # Restore persisted queue jobs (version 3+)
        saved_jobs = d.get("queue_jobs", [])
        if isinstance(saved_jobs, list) and saved_jobs:
            self._next_job_id = int(d.get("next_job_id", 1))
            restored: list[RenderJob] = []
            for jd in saved_jobs:
                if not isinstance(jd, dict):
                    continue
                try:
                    ro_dict = jd.get("render_options")
                    ro: Optional[RenderOptions] = None
                    if isinstance(ro_dict, dict):
                        ro = RenderOptions(**{k: v for k, v in ro_dict.items() if k in RenderOptions.__dataclass_fields__})
                    masn: list[MaterialVideoAssignment] = []
                    for ad in jd.get("material_assignments", []):
                        if isinstance(ad, dict):
                            masn.append(MaterialVideoAssignment(
                                material_name=str(ad.get("material_name", "")),
                                video_path=str(ad.get("video_path", "")),
                                mapping_mode=str(ad.get("mapping_mode", VIDEO_MAPPING_MODE_EMISSION)),
                            ))
                    job = RenderJob(
                        id=int(jd.get("id", self._next_job_id)),
                        label=str(jd.get("label", "")),
                        custom_label=bool(jd.get("custom_label", False)),
                        video_path=str(jd.get("video_path", "")),
                        output_path=str(jd.get("output_path", "")),
                        output_input=str(jd.get("output_input", "")),
                        scene_path=str(jd.get("scene_path", "")),
                        target_camera=str(jd.get("target_camera", "")),
                        output_profile=str(jd.get("output_profile", "H264 MP4")),
                        render_options=ro,
                        safe_mode=bool(jd.get("safe_mode", True)),
                        status=str(jd.get("status", "idle")),
                        progress=float(jd.get("progress", 0.0)),
                        selected=bool(jd.get("selected", True)),
                        use_deadline=bool(jd.get("use_deadline", False)),
                        deadline_pool=str(jd.get("deadline_pool", "")),
                        deadline_secondary_pool=str(jd.get("deadline_secondary_pool", "")),
                        deadline_group=str(jd.get("deadline_group", "")),
                        deadline_priority=int(jd.get("deadline_priority", 50)),
                        deadline_comment=str(jd.get("deadline_comment", "")),
                        deadline_department=str(jd.get("deadline_department", "")),
                        deadline_chunk_size=int(jd.get("deadline_chunk_size", 1)),
                        deadline_suspended=bool(jd.get("deadline_suspended", False)),
                        deadline_submit_scene=bool(jd.get("deadline_submit_scene", True)),
                        deadline_job_name_template=str(jd.get("deadline_job_name_template", "BlenderRender Job - {scene_name}")),
                        deadline_machine_limit=int(jd.get("deadline_machine_limit", 0)),
                        deadline_limits=str(jd.get("deadline_limits", "")),
                        deadline_command_path=str(jd.get("deadline_command_path", "")),
                        deadline_repo_path=str(jd.get("deadline_repo_path", "")),
                        deadline_whitelist=str(jd.get("deadline_whitelist", "")),
                        material_assignments=masn,
                    )
                    # Reset running/cancelled jobs back to idle on reload
                    if job.status in {"running", "cancelled"}:
                        job.status = "idle"
                        job.progress = 0.0
                    restored.append(job)
                except Exception:
                    continue
            if restored:
                self._jobs = restored
                self._next_job_id = max((j.id for j in restored), default=0) + 1
                saved_active = d.get("active_job_id")
                if isinstance(saved_active, int) and any(j.id == saved_active for j in restored):
                    self._active_job_id = saved_active
                self._refresh_job_outputs()
                self._refresh_queue_view()
        else:
            self._sync_jobs()

        state_b64 = str(d.get("layout_state", "")).strip()
        geom_b64 = str(d.get("layout_geometry", "")).strip()
        if geom_b64:
            try:
                if self.restoreGeometry(QByteArray.fromBase64(geom_b64.encode("ascii"))):
                    self._restored_geometry = True
            except Exception:
                pass
        if state_b64:
            try:
                self.restoreState(QByteArray.fromBase64(state_b64.encode("ascii")))
                self._schedule_titlebar_sync()
            except Exception:
                pass

        # Invariant: a non-empty queue always has exactly one active job.
        self._ensure_active_selection()

    def _ensure_active_selection(self) -> None:
        """Guarantee the 'one active job' invariant: if the queue is non-empty
        but nothing is active (e.g. just loaded a saved queue), open the first
        job. Modern open-document behaviour — there's always a focused item."""
        if self._active_job_id is not None and any(j.id == self._active_job_id for j in self._jobs):
            self.queue_panel.select_job(self._active_job_id)
            return
        self._active_job_id = None
        if self._jobs:
            self._active_job_id = self._jobs[0].id
            self.queue_panel.select_job(self._active_job_id)

    def _load_profile(self) -> None:
        self._is_first_run = not PROFILE_PATH.exists()
        if not PROFILE_PATH.exists():
            return
        try:
            self._apply_profile_data(json.loads(PROFILE_PATH.read_text()))
        except Exception:
            pass

    def _maybe_first_run(self) -> None:
        """A one-time welcome on the very first launch: show what's detected and
        point new users at setup + Quick Start."""
        if not getattr(self, "_is_first_run", False):
            return
        blender = _find_blender(self._blender_path)
        lines = [
            f"Blender:  {'✓ ' + Path(blender).name if blender else '✗ not found — set it in Properties'}",
            f"Cinema 4D:  {'✓ detected' if self._c4dpy_path else '— not detected (optional)'}",
        ]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Information)
        box.setWindowTitle(f"Welcome to {APP_NAME}")
        box.setText("You're set up to map videos onto a 3D scene and render headlessly.")
        box.setInformativeText("\n".join(lines) + "\n\nDrop in a scene, add clips, map them, and hit render.")
        qs = box.addButton("Open Quick Start", QMessageBox.ActionRole)
        loc = box.addButton("Locate Blender…", QMessageBox.ActionRole) if not blender else None
        box.addButton("Get Started", QMessageBox.AcceptRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is qs:
            self._show_quick_start()
        elif loc is not None and clicked is loc:
            self._show_properties_dialog()
        self._save_profile()   # ensure a profile exists so this won't show again

    def _save_profile(self) -> None:
        try:
            PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            PROFILE_PATH.write_text(json.dumps(self._profile_dict(), indent=2))
        except Exception:
            pass

    def _schedule_save(self) -> None:
        if self._save_timer is None:
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.timeout.connect(self._save_profile)
        self._save_timer.start(400)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_profile()
        if self._render_thread and self._render_thread.isRunning():
            self._render_thread.request_cancel()
            self._render_thread.wait(3000)
        event.accept()


SINGLE_INSTANCE_KEY = "RenderMapperPro.singleton"


def _set_macos_app_name(name: str) -> None:
    """Set the macOS menu-bar / Dock application name so it reads as the app
    instead of "Python". Uses the Objective-C runtime via ctypes (no pyobjc
    dependency). Must run before QApplication is created. Best-effort."""
    if sys.platform != "darwin":
        return
    try:
        import ctypes
        import ctypes.util

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.restype = ctypes.c_void_p

        def msg(receiver, selector, *args, argtypes=None, restype=ctypes.c_void_p):
            send = objc["objc_msgSend"]
            send.restype = restype
            send.argtypes = [ctypes.c_void_p, ctypes.c_void_p] + (argtypes or [])
            return send(receiver, objc.sel_registerName(selector), *args)

        NSString = objc.objc_getClass(b"NSString")
        def nsstr(s):
            return msg(NSString, b"stringWithUTF8String:", s.encode("utf-8"),
                       argtypes=[ctypes.c_char_p])

        NSBundle = objc.objc_getClass(b"NSBundle")
        main_bundle = msg(NSBundle, b"mainBundle")
        info = msg(main_bundle, b"infoDictionary")
        if info:
            for key in (b"CFBundleName", b"CFBundleDisplayName", b"CFBundleExecutable"):
                msg(info, b"setObject:forKey:", nsstr(name), nsstr(key.decode()),
                    argtypes=[ctypes.c_void_p, ctypes.c_void_p])
    except Exception:
        pass


def _install_crash_handler(window) -> None:
    """Catch unhandled exceptions on the UI thread: log the traceback and show a
    friendly, copyable dialog instead of a silent failure or a hard crash."""
    import traceback as _tb
    default_hook = sys.excepthook
    showing = {"active": False}   # guard against recursive dialogs

    def _hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            default_hook(exc_type, exc, tb)
            return
        text = "".join(_tb.format_exception(exc_type, exc, tb))
        try:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now().isoformat()}] UNHANDLED EXCEPTION\n{text}\n")
        except Exception:
            pass
        if showing["active"]:
            return
        showing["active"] = True
        try:
            box = QMessageBox(window)
            box.setIcon(QMessageBox.Critical)
            box.setWindowTitle(f"{APP_NAME} — Unexpected Error")
            box.setText("Something went wrong. The app will keep running, but the last "
                        "action may not have completed.")
            box.setInformativeText(f"{exc_type.__name__}: {exc}")
            box.setDetailedText(text)
            copy_btn = box.addButton("Copy Details", QMessageBox.ActionRole)
            log_btn = box.addButton("Open Log", QMessageBox.ActionRole)
            box.addButton(QMessageBox.Close)
            box.exec()
            clicked = box.clickedButton()
            if clicked is copy_btn:
                QApplication.clipboard().setText(text)
            elif clicked is log_btn:
                try:
                    reveal_in_file_manager(LOG_PATH)
                except Exception:
                    pass
        except Exception:
            default_hook(exc_type, exc, tb)
        finally:
            showing["active"] = False

    sys.excepthook = _hook


def run_qt_app() -> None:
    _set_macos_app_name(APP_NAME)
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("Toy Robot Media")
    app.setWindowIcon(_make_app_icon())

    # ── Single-instance guard ────────────────────────────────────────────
    # If another copy is already running, ask it to surface its window and
    # exit this one instead of opening a second window.
    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    probe = QLocalSocket()
    probe.connectToServer(SINGLE_INSTANCE_KEY)
    if probe.waitForConnected(250):
        probe.write(b"raise")
        probe.flush()
        if probe.state() == QLocalSocket.ConnectedState:
            probe.waitForBytesWritten(250)
            probe.disconnectFromServer()
        print("Render Mapper Pro is already running — focusing the existing window.", file=sys.stderr)
        return
    # No live instance; clear any stale socket and become the server.
    QLocalServer.removeServer(SINGLE_INSTANCE_KEY)
    server = QLocalServer()
    server.listen(SINGLE_INSTANCE_KEY)

    win = BlenderVideoMapperQt()
    win._single_instance_server = server  # keep a reference alive
    _install_crash_handler(win)  # friendly dialog + log on any unhandled UI-thread error
    win._init_window_geometry()  # place/size before first show to avoid an off-screen flash

    def _on_second_launch() -> None:
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.disconnectFromServer()
        win.setWindowState((win.windowState() & ~Qt.WindowMinimized) | Qt.WindowActive)
        win.show()
        win.raise_()
        win.activateWindow()

    server.newConnection.connect(_on_second_launch)

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run_qt_app()
