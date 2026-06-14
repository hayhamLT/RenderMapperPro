from __future__ import annotations

import dataclasses
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTabBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

import icons
import theme as T
from core.metrics import (
    auto_chunk_size,
    estimate_energy_cost,
    estimate_output_bytes,
    predict_total_seconds,
)
from core.models import (
    VIDEO_MAPPING_MODE_EMISSION,
    JobConfig,
    MaterialVideoAssignment,
    RenderJob,
    RenderOptions,
)
from core.utils import (
    OUTPUT_PROFILES,
    VIDEO_EXTENSIONS,
    ext_for_format,
    file_exists,
    find_deadlinecommand,
    resolve_output_path,
)
from core.utils import update_platform_key as _update_platform_key
from core.utils import version_tuple as _version_tuple
from media import (
    _find_ffprobe,
    _normalize_fps,
    _parse_mp4_info,
    build_contact_sheet,
    find_ffmpeg_tool,
    probe_video_size,
    reveal_in_file_manager,
    video_has_audio,
)
from panels import (
    DeadlinePanel,
    LogsPanel,
    PresetBrowserPanel,
    PreviewPanel,
    QueuePanel,
    RenderPanel,
    ScenePanel,
)
from theme import set_active_palette
from ui_widgets import (
    _ImageView,
)
from workers import (
    DeadlineQueryThread,
    DiscoveryThread,
    ExportBlendThread,
    FuncThread,
    PreviewFrameThread,
    RenderThread,
)

PROFILE_PATH = Path.home() / ".blender_video_mapper" / "profile.json"
PRESETS_DIR = Path.home() / ".blender_video_mapper" / "presets"
HISTORY_PATH = Path.home() / ".blender_video_mapper" / "history.json"
# Branded file extensions (JSON underneath) for user-facing Save/Open.
PROJECT_EXT = ".rmproj"      # full project: scene, clips, mappings, queue
PRESET_EXT = ".rmpreset"     # reusable render-settings recipe
REPORTS_DIR = Path.home() / ".blender_video_mapper" / "reports"
LOG_PATH = Path.home() / ".blender_video_mapper" / "logs" / "app_qt.log"
APP_NAME = "Render Mapper Pro"
APP_VERSION = "1.6.0"
RUNTIME_ROOT = Path.home() / ".blender_video_mapper" / "runtime"
BLENDER_RUNTIME_VERSION = "5.1.0"
PROFILE_VERSION = 3
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

def _make_app_icon() -> QIcon:
    return icons.app_icon()


def _norm_blender(candidate: str) -> str | None:
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


def _managed_blender_executable() -> str | None:
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


def _runtime_download_spec() -> tuple[str, str] | None:
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


def _bundled_asset(name: str) -> Path | None:
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


def _find_blender(preferred: str = "") -> str | None:
    candidates: list[str] = []
    seen: set[str] = set()

    def add(v: str | None) -> None:
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
        add(str(Path.home() / "Library/Application Support/Steam/steamapps/common/"
                              "Blender/Blender.app/Contents/MacOS/Blender"))
    elif os.name == "nt":
        import glob as _glob
        local = os.environ.get("LOCALAPPDATA", "")
        for pat in (r"C:\Program Files\Blender Foundation\Blender *\blender.exe",
                    r"C:\Program Files\Blender Foundation\*\blender.exe",
                    r"C:\Program Files\Blender Foundation\blender.exe",
                    r"C:\Program Files (x86)\Steam\steamapps\common\Blender\blender.exe",
                    r"D:\Steam\steamapps\common\Blender\blender.exe",
                    r"D:\SteamLibrary\steamapps\common\Blender\blender.exe",
                    (local + r"\Programs\Blender Foundation\*\blender.exe") if local else ""):
            if not pat:
                continue
            for hit in sorted(_glob.glob(pat), reverse=True):
                add(hit)
    else:  # linux
        import glob as _glob
        for p in ("/usr/bin/blender", "/usr/local/bin/blender", "/snap/bin/blender",
                  str(Path.home() / ".local/bin/blender")):
            add(p)
        for hit in sorted(_glob.glob("/opt/blender*/blender"), reverse=True):
            add(hit)

    for c in candidates:
        r = _norm_blender(c)
        if r:
            return r
    return None


# Blender versions this app has been tested against. Outside this range still
# runs, but warn the user since render/preview behavior may differ.
BLENDER_MIN_TESTED = (4, 0)
BLENDER_MAX_TESTED = (5, 99)


def _blender_version_status(version_line: str) -> str:
    """Format a 'Blender X.Y.Z' --version line with a compatibility hint."""
    m = re.search(r"(\d+)\.(\d+)", version_line)
    if not m:
        return f"✓ {version_line}"
    mv = (int(m.group(1)), int(m.group(2)))
    if mv < BLENDER_MIN_TESTED:
        return (f"⚠ {version_line} — older than the recommended "
                f"{BLENDER_MIN_TESTED[0]}.{BLENDER_MIN_TESTED[1]}+; may not render correctly.")
    if mv > BLENDER_MAX_TESTED:
        return (f"⚠ {version_line} — newer than the tested range "
                f"({BLENDER_MIN_TESTED[0]}.x–{BLENDER_MAX_TESTED[0]}.x); untested, but worth a try.")
    return f"✓ {version_line}"


def _resolve_runtime_script(name: str) -> str:
    roots = [Path(__file__).parent, Path.cwd()]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(getattr(sys, "_MEIPASS", "")))
    for root in roots:
        c = root / name
        if c.exists() and c.is_file():
            return str(c)
    raise FileNotFoundError(f"Runtime script not found: {name}")


class RuntimeInstallThread(QThread):
    log = Signal(str)
    finished_install = Signal(str, str)

    def _download(self, url: str, dest: Path) -> None:
        self.log.emit(f"[runtime] Downloading {url}")
        self.log.emit("[runtime] This is a ~300–700 MB download and can take several minutes.")
        req = urllib.request.Request(url, headers={"User-Agent": "RenderMapperPro/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as out:
            total = int(resp.headers.get("Content-Length", "0") or "0")
            read = 0
            last_pct = -5
            t0 = time.monotonic()
            mb = 1024 * 1024
            while True:
                chunk = resp.read(1024 * 512)
                if not chunk:
                    break
                out.write(chunk)
                read += len(chunk)
                if total > 0:
                    pct = int((read / total) * 100)
                    if pct >= last_pct + 5:        # 5% steps, not one line per 512 KB
                        last_pct = pct
                        elapsed = max(0.001, time.monotonic() - t0)
                        speed = read / elapsed     # bytes/s
                        eta = (total - read) / speed if speed > 0 else 0
                        self.log.emit(
                            f"[runtime] Download {pct}% — {read // mb}/{total // mb} MB "
                            f"· {speed / mb:.1f} MB/s · ~{int(eta)}s left")

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


class BlenderVideoMapperQt(QMainWindow):
    _update_checked = Signal(object, bool)   # (manifest dict | None, was-manual)
    _delivery_log = Signal(str, str)         # (message, kind) from the delivery-copy thread
    _sheets_built = Signal(int)              # count of contact sheets generated post-render

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(_make_app_icon())
        self.resize(1400, 860)

        self._blender_path = ""
        self._c4dpy_path = _find_c4dpy()   # Cinema 4D headless Python, if installed
        self._deadline_repo_path = ""
        self._deadline_command_path = ""
        self._deadline_job_name_template = "Render Mapper Pro Job - {scene_name}"
        self._deadline_comment = ""
        self._discovered_materials: list[str] = []
        self._discovered_cameras: list[str] = []
        self._jobs: list[RenderJob] = []
        self._next_job_id = 1
        self._is_rendering = False
        self._pending_autorender_ids: set = set()   # auto-render jobs deferred while a render runs
        self._scan_in_progress = False
        # Animated "still working" indicator for long operations (e.g. Scan).
        self._busy_timer: QTimer | None = None
        self._busy_active = False
        self._busy_label = ""
        self._busy_i = 0
        self._known_videos: set[str] = set()
        self._ffmpeg_hint_shown = False
        self._active_job_id: int | None = None
        self._loading_job_into_ui = False

        self._render_thread: RenderThread | None = None
        self._discovery_thread: DiscoveryThread | None = None
        self._runtime_install_thread: RuntimeInstallThread | None = None
        self._runtime_prompted = False
        self._save_timer: QTimer | None = None

        self._theme_mode = "dark"
        self._accent = T.ACCENT_ORANGE
        self._palette: T.Palette = T.build_palette(self._theme_mode, self._accent)
        self._toast: QWidget | None = None
        self._toast_anim: QPropertyAnimation | None = None

        self._preview_enabled = True
        self._preview_path = ""
        self._preview_timer: QTimer | None = None
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
        self._deliver_dir = ""           # blank = no post-render delivery copy
        self._material_aspects: dict = {}   # material → screen aspect (from discovery)
        self._aspect_warned: set = set()    # (material, clip) pairs already warned about
        self._autorender_start = False        # auto-start vs queue-only
        self._last_report_path = ""
        self._last_html_report_path = ""
        self._c4d_force_submit = False   # override the C4D blank-bake guard
        self._single_instance_server: object = None   # set by run_qt_app, kept alive
        self._power_watts = 300.0        # est. machine draw for cost reporting
        self._power_rate = 0.15          # electricity rate ($/kWh)
        self._notify_desktop = True      # system-tray notifications on render events
        self._discord_webhook = ""       # optional Discord webhook for render events
        self._discord_thread: FuncThread | None = None
        self._job_durations: dict[int, float] = {}
        self._job_metrics: dict[int, dict] = {}   # job_id → {frames, avg_spf, p95_spf}
        self._preview_thread: PreviewFrameThread | None = None
        self._deadline_test_thread: DeadlineQueryThread | None = None
        self._props_deadline_thread: DeadlineQueryThread | None = None
        self._farm_nodes_thread: DeadlineQueryThread | None = None
        # Managed background threads (vs. raw daemons) so closeEvent can wait on
        # them — a signal emitted after the app quits would otherwise crash Qt.
        self._update_check_thread: FuncThread | None = None
        self._delivery_thread: FuncThread | None = None
        self._shutting_down = False
        self._update_checked.connect(self._on_update_checked)
        self._delivery_log.connect(self._show_toast)
        self._sheets_built.connect(
            lambda n: self._append_log(f"[app] Generated {n} contact sheet(s).") if n else None)
        self._sheet_thread: FuncThread | None = None
        self._undo_stack: list = []      # (description, restore_callable) for destructive actions

        self._apply_theme()
        self._build_menu()
        self._build_layout()
        self._build_status_bar()
        self._build_tray()
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

    def _toggle_theme(self, light: bool) -> None:
        mode = "light" if light else "dark"
        if mode == self._theme_mode:
            return
        self._theme_mode = mode
        self._restyle_all()
        # Keep the menu checkmark in sync even when toggled programmatically.
        if hasattr(self, "theme_action") and self.theme_action.isChecked() != light:
            self.theme_action.blockSignals(True)
            self.theme_action.setChecked(light)
            self.theme_action.blockSignals(False)
        self._schedule_save()

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
        self._undo_action.setShortcut(QKeySequence.StandardKey.Undo)   # ⌘Z / Ctrl+Z
        self._undo_action.setEnabled(False)
        self._undo_action.triggered.connect(self._undo)
        edit.addAction(self._undo_action)

        profile = mb.addMenu("Profile")
        props_act = profile.addAction("Properties…", self._show_properties_dialog)
        props_act.setShortcut(QKeySequence("Ctrl+,"))   # ⌘, — the macOS settings convention
        profile.addSeparator()
        open_act = profile.addAction("Open Project…", self._open_project)
        open_act.setShortcut(QKeySequence.StandardKey.Open)          # ⌘O / Ctrl+O
        save_act = profile.addAction("Save Project As…", self._save_project)
        save_act.setShortcut(QKeySequence.StandardKey.Save)          # ⌘S / Ctrl+S
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
        self._open_html_action = QAction("Open HTML Render Report", self)
        self._open_html_action.triggered.connect(self._open_html_report)
        self._open_html_action.setEnabled(False)
        tools.addAction(self._open_html_action)
        tools.addSeparator()
        force_act = QAction("Force C4D Submit (ignore blank-bake guard)", self, checkable=True)
        force_act.setToolTip("Submit Cinema 4D farm jobs even when the bake produced no clip "
                             "frames. Off by default — only enable if you know the blank result "
                             "is intentional.")
        force_act.toggled.connect(lambda on: setattr(self, "_c4d_force_submit", on))
        tools.addAction(force_act)
        tools.addAction("Power & Cost Settings…", self._show_power_settings)
        tools.addAction("Notifications…", self._show_notification_settings)
        palette_act = tools.addAction("Command Palette…", self._show_command_palette)
        palette_act.setShortcut(QKeySequence("Ctrl+K"))   # ⌘K on macOS
        tools.addSeparator()
        tools.addAction("Copy Diagnostics", self._copy_diagnostics)
        tools.addSeparator()
        when_menu = tools.addMenu("When Render Finishes")
        when_group = QActionGroup(self)
        when_group.setExclusive(True)
        for label, val in (("Do Nothing", "nothing"), ("Quit App", "quit"), ("Sleep Computer", "sleep")):
            act = QAction(label, self, checkable=True)
            act.setChecked(val == self._when_done)
            act.triggered.connect(lambda _c=False, v=val: self._set_when_done(v))
            when_group.addAction(act)
            when_menu.addAction(act)
        self._when_actions = {a.text(): a for a in when_group.actions()}

        runtime = mb.addMenu("Runtime")
        runtime.addAction("Install Managed Blender", self._install_managed_runtime)
        runtime.addAction("Locate Blender…", self._show_properties_dialog)

        deadline = mb.addMenu("Deadline")
        deadline.addAction("Test Connection", self._test_deadline_connection)
        deadline.addAction("Farm Nodes…", self._show_farm_nodes)
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
        self.theme_action = QAction("Light Theme", self, checkable=True)
        self.theme_action.setChecked(self._theme_mode == "light")
        self.theme_action.toggled.connect(self._toggle_theme)
        self.view_menu.addAction(self.theme_action)
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
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
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
        <p class="muted"><b>Chunking</b> can be <i>Manual</i> or <i>Auto</i> (~5/10/20 min per task) —
        Auto sizes Frames-Per-Task from your render history. <i>Deadline → Farm Nodes…</i> lists the
        nodes on the farm, and right-clicking a queued job offers <b>Set Priority</b> and
        <b>Requeue</b>.</p>
        <h3>Analytics, reports &amp; notifications</h3>
        <p class="muted">Renders record <b>seconds/frame</b>, total time and an estimated <b>cost</b>
        (set machine wattage + rate in <i>Tools → Power &amp; Cost</i>); see them in
        <i>Tools → Render History</i>, which also builds a <b>contact sheet</b> for any output. Each run
        writes an <b>HTML report</b> (<i>Tools → Open HTML Render Report</i>) with timing, cost and
        embedded thumbnails. Get pinged when a render finishes or fails via <i>Tools → Notifications</i>
        — system tray and/or a <b>Discord webhook</b> (everything is also logged to Live Logs).</p>
        <h3>Audio</h3>
        <p class="muted">Any clip that contains sound shows a <b>speaker</b> badge in the Videos
        list. Click the badge (or right-click → <i>Mute audio</i>) to drop that clip's audio; every
        non-muted mapped clip is mixed into the rendered video.</p>
        <h3>Queue</h3>
        <p class="muted">Click a row to activate and edit it; <b>double-click the name</b> to rename.
        New jobs (the <b>+</b> button) are added at the top. Duplicate with <kbd>⌘D</kbd>,
        delete with <kbd>⌫</kbd>, or right-click for Duplicate / Set Priority / Requeue / Reveal /
        Open / Move / Delete. Tick the <b>Run</b> box to include a job when you press Start.</p>
        <h3>Layout &amp; appearance</h3>
        <p class="muted"><i>View → Layout</i> offers Default, All Panels (grid), Render Focus,
        Setup Focus, Stacked and Tabbed presets, plus <b>Save Current Layout</b>. Drag a panel's
        tab to rearrange, tab, or float it, and show/hide panels from <i>View</i>.
        Toggle <i>View → Light Theme</i> for light/dark, and press <kbd>⌘K</kbd> for the
        <b>command palette</b> to search and run any action. The window opens at 70% of your
        screen, centered, and remembers your size and position.</p>
        """
        self._show_help_dialog("Quick Start", html)

    def _show_shortcuts_help(self) -> None:
        rows = [
            ("⌘K", "Command palette — search & run any action"),
            ("⌘O", "Open a project"),
            ("⌘S", "Save the project"),
            ("⌘,", "Properties & Settings"),
            ("⌘Z", "Undo the last destructive action"),
            ("⌘R", "Start render (queued jobs ticked Run)"),
            ("⌘⇧R", "Start all queued jobs"),
            ("⌘.", "Stop the current render"),
            ("⌘B", "Browse for a scene file"),
            ("⌘D", "Duplicate selected queue job(s)"),
            ("⌫ / Delete", "Delete selected queue job(s) / clips"),
            ("Space", "Play/pause the preview movie"),
        ]
        body = "".join(f"<tr><td><kbd>{k}</kbd></td><td>{d}</td></tr>" for k, d in rows)
        html = self._help_css() + f"""
        <h2>Keyboard Shortcuts</h2>
        <table>{body}</table>
        <p class="muted">On Windows/Linux, ⌘ is Ctrl. Queue shortcuts apply when the Queue has focus.</p>
        """
        self._show_help_dialog("Keyboard Shortcuts", html)

    @staticmethod
    def _logo_path() -> Path | None:
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
        lay.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        def centered(w):
            w.setAlignment(Qt.AlignCenter)
            lay.addWidget(w, 0, Qt.AlignHCenter)
            return w

        lay.addWidget(_ImageView(_make_app_icon().pixmap(QSize(92, 92)), pal.window),
                      0, Qt.AlignmentFlag.AlignHCenter)

        name = centered(QLabel(APP_NAME))
        name.setStyleSheet(f"color:{pal.text}; font-size:19px; font-weight:700; margin-top:8px;")
        ver = centered(QLabel(f"Version {APP_VERSION}"))
        ver.setStyleSheet(f"color:{pal.text_muted}; font-size:12px;")
        desc = centered(QLabel("Automated video-texture mapping and\nheadless rendering for Blender."))
        desc.setStyleSheet(f"color:{pal.text_muted}; font-size:12px; margin-top:8px;")
        desc.setWordWrap(True)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color:{pal.border}; margin:14px 40px;")
        lay.addWidget(sep)

        powered = centered(QLabel("Powered by"))
        powered.setStyleSheet(f"color:{pal.text_faint}; font-size:11px;")
        logo = self._logo_path()
        if logo is not None:
            pm = QPixmap(str(logo))
            if not pm.isNull():
                scaled = pm.scaledToWidth(260, Qt.TransformationMode.SmoothTransformation)  # 2× of 130 for Retina
                scaled.setDevicePixelRatio(2.0)
                lay.addWidget(_ImageView(scaled, pal.window), 0, Qt.AlignmentFlag.AlignHCenter)
            else:
                logo = None
        if logo is None:
            brand = centered(QLabel("Toy Robot Media"))
            brand.setStyleSheet(f"color:{pal.text}; font-size:14px; font-weight:700;")

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addSpacing(10)
        lay.addWidget(btns)
        dlg.exec()

    def _build_layout(self) -> None:
        self.setDockOptions(
            QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.GroupedDragging
        )
        # Tabs on top (the tab acts as the panel header) instead of Qt's default
        # bottom placement.
        self.setTabPosition(Qt.DockWidgetArea.AllDockWidgetAreas, QTabWidget.TabPosition.North)

        # No central widget at all: the docks fill the whole window. A zero-size
        # central widget would otherwise leave a phantom, draggable separator
        # pinned to the far-right edge.
        self.setCentralWidget(None)  # type: ignore[arg-type]  # Qt: None clears it

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
        self.scene_panel.watch_changed.connect(lambda *_: self._save_and_refresh_status())
        self.scene_panel.target_set_ready.connect(self._on_target_set_ready)
        self.scene_panel.targets_changed.connect(lambda *_: self._save_profile())
        self.scene_panel.assignments_cleared.connect(
            lambda snap: self._push_undo(f"Clear Mappings ({len(snap)})",
                                         lambda: self.scene_panel.set_assignments(snap)))
        self.scene_panel.videos_removed.connect(
            lambda n, snap: self._push_undo(f"Remove {n} Clip(s)",
                                            lambda: self.scene_panel.restore_videos_snapshot(snap)))
        self.scene_panel.mute_changed.connect(self._schedule_save)
        self.scene_panel.render_requested.connect(self._start_render)

        self.render_panel.output_changed.connect(lambda _v: self._on_settings_changed(preview=False))
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
        self.render_panel.scale_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed(preview=False))
        self.render_panel.quality_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed(preview=False))
        self.render_panel.codec_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed(preview=False))
        self.render_panel.transparent_cb.stateChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_preset_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_min_samples_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_threshold_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_gi_bounces_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_ray_depth_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.rs_gi_cb.toggled.connect(lambda _v: self._on_settings_changed())

        self.deadline_panel.settings_changed.connect(lambda: self._on_settings_changed(preview=False))
        self.deadline_panel.test_connection_requested.connect(self._test_deadline_connection)
        # When the user ticks "Enable Deadline Submission" but it isn't configured,
        # jump them straight to the Deadline settings to fix it (clicked = user only).
        self.deadline_panel.use_dl_cb.clicked.connect(self._on_deadline_enabled_clicked)
        self.deadline_panel.export_requested.connect(self._export_deadline_files)

        self.render_panel.profile_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed(preview=False))
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
        self.queue_panel.show_error_requested.connect(self._show_job_error)
        self.queue_panel.open_output_requested.connect(self._open_job_output)
        self.queue_panel.move_job_requested.connect(self._move_job)
        self.queue_panel.duplicate_jobs_requested.connect(self._duplicate_jobs)
        self.queue_panel.set_priority_requested.connect(self._set_jobs_priority)
        self.queue_panel.requeue_requested.connect(self._requeue_jobs)
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
        widget.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        dock.setWidget(widget)
        # Standard dock behaviour: movable, floatable and closable. Closable is
        # what lets the View-menu toggleViewAction() entries enable/check; without
        # it Qt greys them out because the panel can never be hidden.
        dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
            | QDockWidget.DockWidgetFeature.DockWidgetClosable
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
        lay.addWidget(chip, 0, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom)
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
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

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
        # Pop-up notifications were removed by request. Messages go to the Live
        # Logs panel AND flash briefly in the status bar — so feedback is always
        # visible even when the Logs dock is closed, with no on-screen overlay.
        prefix = {"error": "[error] ", "warning": "[warn] "}.get(kind, "[app] ")
        self._append_log(prefix + message)
        bar = self.statusBar()
        if bar is not None:
            icon = {"error": "✗ ", "warning": "⚠ ", "success": "✓ "}.get(kind, "")
            bar.showMessage(icon + message, 5000)

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
        anim.setEasingCurve(QEasingCurve.Type.InCubic)
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
            self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, d)
            d.show()

        if preset == "grid":
            # Every panel visible at once, none tabbed — three columns each
            # split vertically so all seven docks are on screen together.
            #   col1: Scene / Presets
            #   col2: Render Settings / Deadline Farm
            #   col3: Queue / Live Preview / Live Logs
            self.splitDockWidget(sc, rd, Qt.Orientation.Horizontal)
            self.splitDockWidget(rd, q, Qt.Orientation.Horizontal)
            self.splitDockWidget(sc, pr, Qt.Orientation.Vertical)
            self.splitDockWidget(rd, dl, Qt.Orientation.Vertical)
            self.splitDockWidget(q, pv, Qt.Orientation.Vertical)
            self.splitDockWidget(pv, lg, Qt.Orientation.Vertical)
            self.resizeDocks([sc, rd, q], [440, 440, 560], Qt.Orientation.Horizontal)
            self.resizeDocks([sc, pr], [480, 320], Qt.Orientation.Vertical)
            self.resizeDocks([rd, dl], [480, 320], Qt.Orientation.Vertical)
            self.resizeDocks([q, pv, lg], [360, 320, 180], Qt.Orientation.Vertical)
        elif preset == "focus":
            # Big render-monitoring layout: Scene+Render left, Queue+Preview
            # large on the right, Logs underneath.
            self.splitDockWidget(sc, q, Qt.Orientation.Horizontal)
            self.splitDockWidget(sc, rd, Qt.Orientation.Vertical)
            self.splitDockWidget(q, lg, Qt.Orientation.Vertical)
            self.tabifyDockWidget(rd, dl)
            self.tabifyDockWidget(rd, pr)
            self.tabifyDockWidget(q, pv)
            rd.raise_()
            pv.raise_()
            self.resizeDocks([sc, q], [430, 870], Qt.Orientation.Horizontal)
            self.resizeDocks([q, lg], [640, 220], Qt.Orientation.Vertical)
        elif preset == "setup":
            # Configuration-focused: Scene + Render Settings side by side and
            # large; the render/monitor docks tabbed along the bottom.
            self.splitDockWidget(sc, rd, Qt.Orientation.Horizontal)
            self.splitDockWidget(sc, q, Qt.Orientation.Vertical)
            self.tabifyDockWidget(sc, dl)
            self.tabifyDockWidget(q, pr)
            self.tabifyDockWidget(q, lg)
            self.tabifyDockWidget(q, pv)
            sc.raise_()
            q.raise_()
            self.resizeDocks([sc, rd], [620, 620], Qt.Orientation.Horizontal)
            self.resizeDocks([sc, q], [560, 240], Qt.Orientation.Vertical)
        elif preset == "tabbed":
            # Single pane: every panel tabbed into one stack, maximising the
            # working area of whichever panel is active.
            for d in (rd, dl, q, pr, lg, pv):
                self.tabifyDockWidget(sc, d)
            sc.raise_()
        elif preset == "stacked":
            # Two columns: Scene left, everything else tabbed on the right.
            self.splitDockWidget(sc, rd, Qt.Orientation.Horizontal)
            for d in (dl, q, pr, lg, pv):
                self.tabifyDockWidget(rd, d)
            rd.raise_()
            self.resizeDocks([sc, rd], [430, 1040], Qt.Orientation.Horizontal)
        else:  # "default" — three columns
            self.splitDockWidget(sc, rd, Qt.Orientation.Horizontal)
            self.splitDockWidget(rd, q, Qt.Orientation.Horizontal)
            self.splitDockWidget(rd, pr, Qt.Orientation.Vertical)
            self.splitDockWidget(q, lg, Qt.Orientation.Vertical)
            self.tabifyDockWidget(rd, dl)
            self.tabifyDockWidget(q, pv)
            rd.raise_()
            q.raise_()
            self.resizeDocks([sc, rd, q], [380, 480, 620], Qt.Orientation.Horizontal)
            self.resizeDocks([rd, pr], [520, 240], Qt.Orientation.Vertical)
            self.resizeDocks([q, lg], [640, 200], Qt.Orientation.Vertical)

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
        self._custom_layout_state = bytes(self.saveState().toBase64().data()).decode("ascii")
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

    def _ensure_blender(self, interactive: bool = False) -> str | None:
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
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._install_managed_runtime()
            return None
        if ans == QMessageBox.StandardButton.No:
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
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self._install_managed_runtime()

    def _install_managed_runtime(self) -> None:
        if self._runtime_install_thread and self._runtime_install_thread.isRunning():
            QMessageBox.information(self, "Runtime", "Runtime installation is already in progress.")
            return

        self._append_log(f"[runtime] Installing Blender {BLENDER_RUNTIME_VERSION}...")
        if hasattr(self, "logs_dock"):   # surface the download progress
            self.logs_dock.show()
            self.logs_dock.raise_()
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

    def _deadline_config_missing(self) -> str:
        """Return a short reason the Deadline config is unusable, or '' if it's fine."""
        if not self._deadline_repo_path.strip():
            return "No Deadline repository path is set."
        cmd = self._deadline_command_path.strip() or find_deadlinecommand()
        if not cmd or not (Path(cmd).exists() or shutil.which(cmd)):
            return "deadlinecommand was not found."
        return ""

    def _on_deadline_enabled_clicked(self, checked: bool) -> None:
        """User ticked 'Enable Deadline Submission' — verify it's configured AND the
        farm responds (off-thread, no UI freeze); if not, open Properties →
        Deadline so they can fix it."""
        if not checked:
            return
        reason = self._deadline_config_missing()
        if reason:
            self._append_log(f"[app] Deadline not ready: {reason} Opening Deadline settings.")
            self._show_properties_dialog(initial_tab="Deadline")
            return
        self._test_deadline_connection(interactive=False)

    def _show_properties_dialog(self, initial_tab: str | None = None) -> None:
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
                    QMessageBox.warning(
                        dlg, "Not a Blender App",
                        "That location doesn't contain Blender. Pick the Blender app itself "
                        "(e.g. /Applications/Blender.app) or its executable.")
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
                blender_ver_lbl.setText(
                    _blender_version_status(first) if first else "Could not read Blender version.")
            except Exception as exc:
                blender_ver_lbl.setText(f"Version check failed: {exc}")

        def do_autodetect() -> None:
            found = _find_blender(blender_edit.text().strip())
            if found:
                blender_edit.setText(found)
                do_check_version()
            else:
                blender_ver_lbl.setText(
                    "No Blender found in the usual locations. Use “Locate” to pick it "
                    "manually, or set the BLENDER_PATH environment variable.")

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
        ar_enable_cb = QCheckBox("Auto-render once every render-target screen has a clip")
        ar_enable_cb.setChecked(self._autorender_enabled)
        ar_enable_cb.setToolTip("Mark screens as render targets (right-click a material, or click its "
                                "left stripe). When every target has a clip — or newer versions arrive — "
                                "a single render covering all targets is queued (debounced).")
        lay.addWidget(ar_enable_cb)
        lay.addWidget(hint("Mark targets by right-clicking a material → Set as Render Target, or click the "
                           "stripe on its left. Linking a clip also targets it. The render waits until "
                           "every target has a clip."))
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

        lay.addWidget(section_title("DELIVERY"))
        lay.addWidget(hint("After a render finishes, copy the output(s) into this folder "
                           "automatically — e.g. a synced delivery/review folder. Blank = off."))
        dlv_row = QHBoxLayout()
        dlv_edit = QLineEdit(self._deliver_dir)
        dlv_edit.setPlaceholderText("Delivery folder (blank = no copy)")
        dlv_browse = QPushButton("Browse")
        def _pick_dlv() -> None:
            d2 = QFileDialog.getExistingDirectory(dlg, "Delivery folder", dlv_edit.text() or str(Path.home()))
            if d2:
                dlv_edit.setText(d2)
        dlv_browse.clicked.connect(_pick_dlv)
        dlv_row.addWidget(QLabel("Copy to:"))
        dlv_row.addWidget(dlv_edit, 1)
        dlv_row.addWidget(dlv_browse)
        lay.addLayout(dlv_row)
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

        repo_help = QLabel(
            "Repository Path is your Deadline repository folder — e.g. "
            "/opt/Thinkbox/Deadline/repository, or \\\\server\\DeadlineRepository on "
            "Windows. Leave it blank to use this machine's configured default. "
            "Command Path is auto-detected; set it only if deadlinecommand isn't found. "
            "Use Test Connection to verify — any failure shows full details in Live Logs.")
        repo_help.setWordWrap(True)
        repo_help.setStyleSheet(f"color:{self._palette.text_muted}; font-size:11px;")
        lay.addWidget(repo_help)

        # Name Template
        template_row = QHBoxLayout()
        template_edit = QLineEdit(self._deadline_job_name_template)
        template_edit.setPlaceholderText("e.g. Render Mapper Pro Job - {scene_name}")
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

        # ── Diagnostics ──────────────────────────────────────────────────
        lay = _tab("Diagnostics")
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

        # Dialog buttons live below the tabs.
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
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
            self._deliver_dir = dlv_edit.text().strip()

            self._save_profile()    # persist immediately so settings survive a quick quit
            dlg.accept()

        btns.accepted.connect(on_accept)
        btns.rejected.connect(dlg.reject)

        # Handle testing connection and exporting files inside dialog. The query
        # runs off-thread (DeadlineQueryThread) so the dialog never freezes — the
        # modal event loop still delivers the result signal while it's open.
        def _apply_props_test_result(res: dict) -> None:
            ok = bool(res.get("ok"))
            dp = self.deadline_panel
            if ok:
                pools = res.get("pools", [])
                current_pool = dp.dl_pool_combo.currentText()
                current_sec_pool = dp.dl_sec_pool_combo.currentText()
                dp.dl_pool_combo.clear()
                dp.dl_sec_pool_combo.clear()
                dp.dl_pool_combo.addItems(pools)
                dp.dl_sec_pool_combo.addItems([""] + pools)
                if current_pool:
                    dp.dl_pool_combo.setCurrentText(current_pool)
                if current_sec_pool:
                    dp.dl_sec_pool_combo.setCurrentText(current_sec_pool)

                groups = res.get("groups", [])
                current_group = dp.dl_group_combo.currentText()
                dp.dl_group_combo.clear()
                dp.dl_group_combo.addItems([""] + groups)
                if current_group:
                    dp.dl_group_combo.setCurrentText(current_group)

                machines = res.get("machines", [])
                dp.dl_machines_list.blockSignals(True)
                currently_checked = {
                    dp.dl_machines_list.item(i).text().strip()
                    for i in range(dp.dl_machines_list.count())
                    if dp.dl_machines_list.item(i).checkState() == Qt.CheckState.Checked
                }
                dp.dl_machines_list.clear()
                for m in machines:
                    item = QListWidgetItem(m)
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(
                        Qt.CheckState.Checked if (m in currently_checked or not currently_checked)
                        else Qt.CheckState.Unchecked)
                    dp.dl_machines_list.addItem(item)
                dp.dl_machines_list.blockSignals(False)

            # Dialog feedback — guarded, since the dialog may be closed mid-test.
            try:
                test_conn_btn.setEnabled(True)
                if ok:
                    status_lbl.setText("Connection status: Connected")
                    status_lbl.setStyleSheet(f"color: {self._palette.success}; font-size: 11px; font-weight: bold;")
                    QMessageBox.information(dlg, "Deadline Connection", "Successfully connected to Deadline repository and updated pools, groups, and machine list!")
                else:
                    status_lbl.setText("Connection status: Connection failed")
                    status_lbl.setStyleSheet(f"color: {self._palette.danger}; font-size: 11px; font-weight: bold;")
                    QMessageBox.warning(dlg, "Deadline Warning",
                                        res.get("error", "") or "deadlinecommand failed.")
            except RuntimeError:
                pass   # dialog already closed

        def run_test_connection() -> None:
            cmd = cmd_edit.text().strip() or find_deadlinecommand() or "deadlinecommand"
            status_lbl.setText("Connection status: Testing...")
            status_lbl.setStyleSheet(f"color: {self._palette.warning}; font-size: 11px; font-weight: bold;")
            if not Path(cmd).exists() and not shutil.which(cmd):
                status_lbl.setText("Connection status: deadlinecommand not found")
                status_lbl.setStyleSheet(f"color: {self._palette.danger}; font-size: 11px; font-weight: bold;")
                QMessageBox.critical(dlg, "Deadline Connection Error", f"deadlinecommand not found at {cmd}.\nPlease check your Thinkbox Deadline installation.")
                return
            if self._props_deadline_thread is not None and self._props_deadline_thread.isRunning():
                return
            test_conn_btn.setEnabled(False)
            self._props_deadline_thread = DeadlineQueryThread(cmd, repo_edit.text().strip())
            self._props_deadline_thread.result.connect(_apply_props_test_result)
            self._props_deadline_thread.start()

        test_conn_btn.clicked.connect(run_test_connection)
        export_files_btn.clicked.connect(self._export_deadline_files)

        if initial_tab:
            for i in range(tabs.count()):
                if tabs.tabText(i) == initial_tab:
                    tabs.setCurrentIndex(i)
                    break

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
            blender = self._ensure_blender(interactive=True) or ""
            if not blender:
                return
        self._add_recent_scene(scene)
        if self._scan_in_progress:
            return

        self._scan_in_progress = True
        self.scene_panel.scan_btn.setEnabled(False)
        self._append_log("[app] Scanning scene...")
        # Headless Blender can take 10-60s to open a heavy scene; the animated
        # spinner + busy cursor make clear it's working, not frozen.
        self._begin_busy(f"Scanning {Path(scene).name}")

        try:
            script = _resolve_runtime_script("blender_discover.py")
            c4d_script = _resolve_runtime_script("c4d_discover.py") if is_c4d else ""
        except Exception as exc:
            self._append_log(f"[app] ERROR: {exc}")
            self.scene_panel.scan_btn.setEnabled(True)
            self._scan_in_progress = False
            self._end_busy()
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

        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

        def _fetch(use_token: bool):
            import urllib.request
            headers = {"Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28",
                       "User-Agent": APP_NAME}
            if use_token:
                headers["Authorization"] = f"Bearer {token}"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))

        def work():
            info = None
            try:
                info = _fetch(use_token=True)
            except Exception:
                # Token may be revoked or rate-limited — fall back to the public
                # API (works if the repo is/becomes public) so updates degrade
                # gracefully instead of breaking on a single embedded credential.
                try:
                    info = _fetch(use_token=False)
                except Exception:
                    info = None
            self._update_checked.emit(info, manual)

        self._update_check_thread = FuncThread(work)
        self._update_check_thread.start()

    def _on_update_checked(self, info, manual: bool) -> None:
        if self._shutting_down:
            return
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
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Update Available")
        box.setText(f"{APP_NAME} {tag} is available — you have v{APP_VERSION}.")
        notes = str(info.get("body") or "").strip()
        if notes:
            box.setInformativeText(notes[:600] + ("…" if len(notes) > 600 else ""))
        get = box.addButton("Download Update", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is get:
            self._fetch_update(info)

    def _fetch_update(self, info) -> None:
        want = self._ASSET_FOR_PLATFORM.get(_update_platform_key()) or ""
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
        if self.render_panel.engine_values() == items:
            return
        cur = self.render_panel.engine_value()
        target = detected if detected in items else (cur if cur in items else items[0])
        combo.blockSignals(True)
        self.render_panel.populate_engines(items)
        self.render_panel.set_engine_value(target)
        combo.blockSignals(False)

    def _on_discovery(self, materials: list, cameras: list, settings: dict) -> None:
        self._material_aspects = dict(settings.get("material_aspects") or {})
        self._aspect_warned.clear()
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
        scene = self.scene_panel.scene_edit.text().strip()
        scene_name = Path(scene).name or "the scene"
        # Distinguish "gone" from "present but unreadable" (cloud sync / permissions).
        detail = ("The file may be from a newer Blender/Cinema 4D version, still syncing "
                  "from Dropbox, or moved. Try opening it in the DCC once, then Rescan.")
        if scene:
            p = Path(scene).expanduser()
            if not p.exists():
                detail = ("The scene file isn't at that path anymore — it may have moved or a "
                          "network/cloud drive isn't mounted. Use Browse to re-locate it.")
            elif not os.access(str(p), os.R_OK):
                detail = ("The scene file exists but can't be read — likely still syncing from "
                          "the cloud, or a permissions issue. Wait for sync to finish, then Rescan.")
                self._append_log(f"[app] Scene not readable (os.R_OK failed): {p}")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle("Scan Failed")
        box.setText(f"Couldn't read materials and cameras from {scene_name}.")
        box.setInformativeText(detail + "\n\nTechnical details below.")
        box.setDetailedText(err)
        rescan = box.addButton("Rescan", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is rescan:
            self._scan_scene()

    _BUSY_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def _begin_busy(self, label: str) -> None:
        """Show an animated spinner + busy cursor in the status bar for a
        long-running operation, so the app never looks frozen."""
        self._busy_label = label
        if self._busy_active:
            return
        self._busy_active = True
        self._busy_i = 0
        if self._busy_timer is None:
            self._busy_timer = QTimer(self)
            self._busy_timer.timeout.connect(self._tick_busy)
        self._busy_timer.start(110)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._tick_busy()

    def _tick_busy(self) -> None:
        frame = self._BUSY_FRAMES[self._busy_i % len(self._BUSY_FRAMES)]
        self._busy_i += 1
        self.statusBar().showMessage(f"{frame}  {self._busy_label} — this can take a minute…")

    def _end_busy(self) -> None:
        if not self._busy_active:
            return
        self._busy_active = False
        if self._busy_timer is not None:
            self._busy_timer.stop()
        QApplication.restoreOverrideCursor()
        self.statusBar().clearMessage()

    def _on_discovery_done(self) -> None:
        self._scan_in_progress = False
        self.scene_panel.scan_btn.setEnabled(True)
        self._end_busy()

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

    def _check_aspect_mismatches(self, assignments: list) -> None:
        """Warn (once per pairing) when a clip's aspect is far from its screen's —
        auto-map is silent, so this is its seatbelt. Warn-only, never blocks."""
        for a in assignments:
            key = (a.material_name, a.video_path)
            if key in self._aspect_warned:
                continue
            screen = self._material_aspects.get(a.material_name)
            size = probe_video_size(a.video_path)
            if not screen or not size:
                continue
            clip = max(size) / min(size)
            if abs(clip - screen) / max(screen, 0.01) > 0.15:
                self._aspect_warned.add(key)
                self._show_toast(
                    f"{Path(a.video_path).stem} is {size[0]}×{size[1]} (~{clip:.2f}:1) but "
                    f"screen '{a.material_name}' is ~{screen:.2f}:1 — it will stretch.",
                    "warning")
            else:
                self._aspect_warned.add(key)   # checked and fine; don't re-probe

    def _on_assignments_changed(self, _asn: list[MaterialVideoAssignment]) -> None:
        # Auto-draft: the moment there's a real mapping, materialise it as a live
        # job so edits are always captured and switching jobs never loses work.
        self._check_aspect_mismatches(_asn)
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
        if self._autorender_start:
            if self._is_rendering:
                self._pending_autorender_ids.add(job.id)   # a render is busy — start it when that finishes
                self._append_log("[app] Auto-render will start when the current render finishes.")
            else:
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
        job.safe_mode = self.render_panel.safe_mode_cb.isChecked()
        job.use_deadline = self.deadline_panel.use_dl_cb.isChecked()
        job.deadline_pool = self.deadline_panel.dl_pool_combo.currentText().strip()
        job.deadline_secondary_pool = self.deadline_panel.dl_sec_pool_combo.currentText().strip()
        job.deadline_group = self.deadline_panel.dl_group_combo.currentText().strip()
        job.deadline_priority = self.deadline_panel.dl_prio_spin.value()
        job.deadline_comment = self._deadline_comment
        job.deadline_department = self.deadline_panel.dl_dept_edit.text().strip()
        job.deadline_chunk_size = self._effective_chunk_size(job)
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
            self.render_panel.burn_in_cb.setChecked(bool(getattr(opts, "burn_in", False)))

            self.render_panel.set_engine_value(opts.engine)

            pidx = self.render_panel.profile_combo.findText(job.output_profile or "H264 MP4")
            if pidx >= 0:
                self.render_panel.profile_combo.setCurrentIndex(pidx)

            self.render_panel.output_edit.setText(job.output_input or "")
        finally:
            self._loading_job_into_ui = False

    def _on_settings_changed(self, preview: bool = True) -> None:
        # Auto-preview works off the live UI, so trigger it regardless of whether
        # there's an active queue job (a camera/resolution change should refresh
        # the preview even before anything is queued). Farm-only settings (Deadline
        # pool/group/etc.) pass preview=False — they don't change the rendered frame,
        # so they must NOT spin up a Blender preview render.
        self._update_status_bar()
        if preview:
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
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
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

    def _job_output_target(self, job_id: int) -> Path | None:
        job = next((j for j in self._jobs if j.id == job_id), None)
        if not job or not (job.output_path or "").strip():
            return None
        p = Path(job.output_path).expanduser()
        if not p.exists():
            p = p if p.suffix else p
            if not p.exists():
                return None
        return p

    @staticmethod
    def _friendly_error_hint(text: str) -> str:
        """Map common renderer failures to a plain-language 'what to try' line.
        Returns "" when nothing matches (the raw error is always shown anyway)."""
        t = (text or "").lower()
        rules = [
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
        for needles, hint in rules:
            if any(n in t for n in needles):
                return hint
        return ""

    def _show_job_error(self, job_id: int) -> None:
        job = next((j for j in self._jobs if j.id == job_id), None)
        if job is None:
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Why This Job Failed")
        box.setText(f"{job.label or f'Job {job.id}'} failed.")
        info = "The last error from the renderer is below. The full output is in Live Logs."
        hint = self._friendly_error_hint(job.error or "")
        if hint:
            info = f"What to try:  {hint}\n\n{info}"
        box.setInformativeText(info)
        box.setDetailedText(job.error or "No error text was captured — check Live Logs.")
        box.exec()

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

    def _set_jobs_priority(self, job_ids: object) -> None:
        ids = [j for j in job_ids if isinstance(j, int)] if isinstance(job_ids, (list, tuple, set)) else []
        if not ids:
            return
        cur = next((j.deadline_priority for j in self._jobs if j.id in ids), 50)
        val, ok = QInputDialog.getInt(self, "Set Priority", "Deadline priority (0–100):", cur, 0, 100)
        if not ok:
            return
        for j in self._jobs:
            if j.id in ids:
                j.deadline_priority = val
        self._schedule_save()
        self._show_toast(f"Priority set to {val} for {len(ids)} job(s).", "info")

    def _requeue_jobs(self, job_ids: object) -> None:
        ids = [j for j in job_ids if isinstance(j, int)] if isinstance(job_ids, (list, tuple, set)) else []
        n = 0
        for j in self._jobs:
            if j.id in ids and j.status in ("failed", "cancelled", "success"):
                j.status = "idle"
                j.progress = 0.0
                j.error = ""
                j.selected = True
                n += 1
        if n:
            self._refresh_queue_view()
            self._show_toast(f"Requeued {n} job(s) — press Render to run them again.", "info")

    def _duplicate_jobs(self, job_ids: object) -> None:
        if self._is_rendering:
            return
        seq = job_ids if isinstance(job_ids, (list, tuple, set)) else []
        ids = [j for j in seq if isinstance(j, int)]
        if not ids:
            return
        last_new_id: int | None = None
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
            errs.append("Blender wasn't found — set it in Properties → Render Engines "
                        "(or use Install Managed Blender).")
        scene = self.scene_panel.scene_edit.text().strip()
        if not scene:
            errs.append("No scene loaded — drop or pick a scene file in the Scene panel.")
        elif not file_exists(scene):
            errs.append(f"The scene file is missing or moved ({Path(scene).name}) — "
                        "re-pick it in the Scene panel.")
        if not self._jobs:
            errs.append("The queue is empty — map a clip to a material and a job "
                        "appears automatically.")
        if not self.render_panel.output_edit.text().strip():
            errs.append("No output path set — enter one in Render Settings → Output Path.")

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

    def _estimate_job_bytes(self, job: RenderJob) -> int:
        opts = job.render_options
        if not opts:
            return 0
        from core.utils import ext_for_format
        is_video = bool(ext_for_format(opts.output_format))
        step = max(1, getattr(opts, "frame_step", 1))
        frames = max(1, (opts.frame_end - opts.frame_start) // step + 1)
        return estimate_output_bytes(
            opts.width, opts.height, frames,
            is_video=is_video, quality=getattr(opts, "video_quality", "HIGH"),
            image_format=opts.output_format,
            scale_percent=getattr(opts, "resolution_percentage", 100))

    def _disk_space_warnings(self, pending: list[RenderJob]) -> list[str]:
        warns: list[str] = []
        by_dir: dict[str, int] = {}
        dpaths: dict[str, Path] = {}
        for j in pending:
            out = (j.output_path or "").strip()
            if not out:
                continue
            d = Path(out).expanduser().parent
            while not d.exists() and d.parent != d:
                d = d.parent
            if not d.exists():
                continue
            key = str(d)
            dpaths[key] = d
            by_dir[key] = by_dir.get(key, 0) + self._estimate_job_bytes(j)
        for key, est in by_dir.items():
            try:
                free = shutil.disk_usage(key).free
            except Exception:
                continue
            gb = 1024 ** 3
            if est and free < est * 1.15:
                warns.append(
                    f"“{dpaths[key]}” may run out of room: ~{est / gb:.1f} GB estimated, "
                    f"only {free / gb:.1f} GB free.")
            elif free < 2 * gb:
                warns.append(f"Low disk space on “{dpaths[key]}”: {free / gb:.1f} GB free.")
        return warns

    def _recent_spf_for_scene(self, scene_path: str) -> float:
        """Most-recent measured sec/frame for this scene, from history (0 if none)."""
        name = Path(scene_path).name
        for h in self._load_history():   # newest-first
            if h.get("scene") == name and h.get("avg_spf"):
                return float(h["avg_spf"])
        return 0.0

    def _effective_chunk_size(self, job: RenderJob) -> int:
        """Manual Frames-Per-Task, or an Auto value sized from render history."""
        manual = self.deadline_panel.dl_chunk_spin.value()
        target_min = self.deadline_panel.chunk_target_minutes()
        if target_min <= 0:
            return manual
        opts = job.render_options
        if opts is None:
            return manual
        fc = max(1, opts.frame_end - opts.frame_start + 1)
        spf = self._recent_spf_for_scene(job.scene_path)
        auto = auto_chunk_size(target_min, spf, fc)
        if auto:
            self._append_log(f"[deadline] Auto chunk: {auto} frames/task "
                             f"(~{target_min:.0f} min at {spf:.1f}s/frame).")
            return auto
        self._append_log("[deadline] Auto chunk: no timing history for this scene yet — "
                         f"using manual ({manual}).")
        return manual

    def _deadline_warnings(self, pending: list[RenderJob]) -> list[str]:
        """Warn before submit if a job's Deadline pool/group isn't in the farm's
        fetched lists (a typo or stale value the farm will reject)."""
        dp = self.deadline_panel
        avail_pools = {dp.dl_pool_combo.itemText(i) for i in range(dp.dl_pool_combo.count())}
        avail_groups = {dp.dl_group_combo.itemText(i) for i in range(dp.dl_group_combo.count())}
        warns: list[str] = []
        seen: set[str] = set()
        for j in pending:
            if not getattr(j, "use_deadline", False):
                continue
            pool = getattr(j, "deadline_pool", "")
            group = getattr(j, "deadline_group", "")
            if pool and avail_pools and pool not in avail_pools and f"pool:{pool}" not in seen:
                seen.add(f"pool:{pool}")
                warns.append(f"Deadline pool “{pool}” isn't in the farm's pool list — the job "
                             "may be rejected. Re-run Test Connection to refresh the pools.")
            if group and avail_groups and group not in avail_groups and f"grp:{group}" not in seen:
                seen.add(f"grp:{group}")
                warns.append(f"Deadline group “{group}” isn't in the farm's group list.")
        return warns

    @staticmethod
    def _unique_path(path: str | Path) -> str:
        # NOTE: " (2)" style, NOT "_v2" — a _vN suffix would read as a clip
        # VERSION to the watch-folder/auto-map pipeline (latest-version-wins).
        path = Path(path)
        parent = path.parent
        if path.suffix:
            stem, ext = path.stem, path.suffix
            i = 2
            while (parent / f"{stem} ({i}){ext}").exists():
                i += 1
            return str(parent / f"{stem} ({i}){ext}")
        base, i = path.name, 2
        while (parent / f"{base} ({i})").exists():
            i += 1
        return str(parent / f"{base} ({i})")

    def _resolve_output_conflicts(self, pending: list[RenderJob]) -> bool:
        """Return True to proceed. Detects existing outputs and lets the user
        Overwrite, Auto-rename (keep both), or Cancel."""
        # Two queued jobs resolving to the SAME path would silently overwrite each
        # other (neither exists on disk yet), so de-dupe within the batch first.
        def _key(pth) -> str:
            return os.path.normcase(os.path.abspath(os.path.expanduser(str(pth))))

        seen_paths: set[str] = set()
        for j in pending:
            p = (j.output_path or "").strip()
            if not p:
                continue
            if _key(p) in seen_paths:
                newp = self._unique_path(Path(p).expanduser())
                while _key(newp) in seen_paths:            # also dodge already-renamed siblings
                    newp = self._unique_path(Path(newp))
                j.output_path = newp
                self._append_log(f"[app] Two jobs shared an output name — renamed one to "
                                 f"{Path(j.output_path).name}")
            seen_paths.add(_key(j.output_path))

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
        ow = box.addButton("Overwrite", QMessageBox.ButtonRole.DestructiveRole)
        rn = box.addButton("Auto-rename", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is ow:
            return True
        if clicked is rn:
            for j, pth in existing:
                j.output_path = self._unique_path(pth)
            self._refresh_queue_view()
            return True
        return False

    def _start_render(self, render_all: bool = False, only_job_ids: set | None = None) -> None:
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
            blender = self._ensure_blender(interactive=True) or ""
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

        # Claim the rendering state NOW — the modal dialogs below spin the Qt event
        # loop, so without this an auto-render signal could re-enter _start_render
        # and double-start (orphaning a RenderThread). Reset on every early return.
        self._is_rendering = True

        # Non-blocking warning if the frame range overshoots the source video,
        # the output disk looks too small, or a Deadline pool/group is unknown.
        warnings = (self._frame_range_warnings(pending)
                    + self._disk_space_warnings(pending)
                    + self._deadline_warnings(pending))
        if warnings:
            ans = QMessageBox.warning(
                self, "Frame Range Warning",
                "\n\n".join(warnings) + "\n\nProceed anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes,
            )
            if ans != QMessageBox.StandardButton.Yes:
                self._is_rendering = False
                return

        # Overwrite protection.
        if not self._resolve_output_conflicts(pending):
            self._is_rendering = False
            return

        try:
            worker = _resolve_runtime_script("blender_worker.py")
            c4d_worker = _resolve_runtime_script("c4d_worker.py") if _is_c4d else ""
        except Exception as exc:
            QMessageBox.critical(
                self, "App Component Missing",
                "The app's render component couldn't be found — the installation may be "
                f"damaged. Reinstall Render Mapper Pro.\n\nDetails: {exc}")
            self._is_rendering = False
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
                force_submit=self._c4d_force_submit,
            )
            entries.append({"id": j.id, "label": j.label, "cfg": cfg})

        self._is_rendering = True
        self.queue_panel.render_selected_btn.setEnabled(False)
        self.queue_panel.render_all_btn.setEnabled(False)
        self.queue_panel.queue_btn.setEnabled(False)

        self._job_started = {}
        self._job_metrics = {}
        self._render_t0 = time.monotonic()
        self.queue_panel.set_progress(0, "Starting…")
        self._log_eta_prediction(entries)

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
        self._render_thread.frame_metrics.connect(self._on_frame_metrics)
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
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes) == QMessageBox.StandardButton.Yes

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
        if self._preview_thread is not None and self._preview_thread.isRunning():
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
            blender = self._ensure_blender(interactive=True) or ""
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

    def _on_frame_metrics(self, job_id: int, frames_done: int, avg_spf: float, p95_spf: float) -> None:
        self._job_metrics[job_id] = {"frames": frames_done, "avg_spf": avg_spf, "p95_spf": p95_spf}
        if self._is_rendering:
            self._update_progress_caption()

    @staticmethod
    def _load_history() -> list[dict]:
        try:
            if HISTORY_PATH.exists():
                data = json.loads(HISTORY_PATH.read_text())
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    def _log_eta_prediction(self, entries: list[dict]) -> None:
        """Before a render, estimate total time from prior runs of the same
        scene(s) and log it — so the user can decide whether to leave it running."""
        history = self._load_history()
        total = 0.0
        have_any = False
        for e in entries:
            cfg = e.get("cfg")
            if cfg is None:
                continue
            scene = Path(getattr(cfg, "scene_path", "") or "").name
            fc = max(1, cfg.render.frame_end - cfg.render.frame_start + 1)
            pred = predict_total_seconds(history, scene, fc)
            if pred is not None:
                total += pred
                have_any = True
        if have_any:
            self._append_log(
                f"[app] Estimated ~{self._fmt_dur(total)} for this render "
                "(from previous runs of these scenes).")

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
        metrics = self._job_metrics.get(job.id, {})
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "label": job.label,
            "scene": Path(job.scene_path).name if job.scene_path else "",
            "scene_full": job.scene_path or "",
            "output": job.output_path,
            "status": status,
            "frames": (f"{opts.frame_start}-{opts.frame_end}" if opts else ""),
            "frame_count": metrics.get("frames", 0),
            "duration": round(duration, 1),
            "avg_spf": round(metrics.get("avg_spf", 0.0), 3),
            "p95_spf": round(metrics.get("p95_spf", 0.0), 3),
        }
        kwh, cost = estimate_energy_cost(duration, self._power_watts, self._power_rate)
        entry["kwh"] = round(kwh, 3)
        entry["cost"] = round(cost, 2)
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
            m = self._job_metrics.get(cur.id)
            if m and m.get("avg_spf"):
                caption += f" · {m['avg_spf']:.1f}s/frame avg"
                if m.get("p95_spf"):
                    caption += f" (p95 {m['p95_spf']:.1f}s)"
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
        self._deliver_outputs()
        self._generate_contact_sheets()

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
        self._render_thread = None   # don't hold a finished thread reference

        # Start any auto-render that arrived while this render was busy.
        if self._autorender_start and self._pending_autorender_ids:
            ids = {jid for jid in self._pending_autorender_ids if any(j.id == jid for j in self._jobs)}
            self._pending_autorender_ids.clear()
            if ids:
                self._append_log("[app] Starting deferred auto-render…")
                self._start_render(only_job_ids=ids)
                return   # _run_when_done_action will fire after that render

        self._run_when_done_action()

    # ── Notifications / when-done / reports ──────────────────────────────
    def _notify(self, title: str, message: str) -> None:
        """Fan an event out to every enabled channel: Live Logs always (the
        no-popup-safe surface), the system tray if enabled, and a Discord webhook
        if configured — so unattended overnight renders can reach you."""
        self._append_log(f"[notify] {title} — {message}")
        tray = getattr(self, "_tray", None)
        if self._notify_desktop and tray is not None and QSystemTrayIcon.supportsMessages():
            tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 6000)
        self._dispatch_discord(f"**{title}** — {message}")

    def _dispatch_discord(self, content: str) -> None:
        """POST a message to the configured Discord webhook off-thread (no-op if
        unset). Failures are surfaced to Live Logs, never raised."""
        url = (self._discord_webhook or "").strip()
        if not url:
            return

        def work() -> None:
            try:
                import urllib.request
                data = json.dumps({"content": content[:1900]}).encode("utf-8")
                req = urllib.request.Request(
                    url, data=data,
                    headers={"Content-Type": "application/json", "User-Agent": APP_NAME})
                urllib.request.urlopen(req, timeout=10).close()
            except Exception as exc:
                self._delivery_log.emit(f"Discord notification failed: {exc}", "error")

        self._discord_thread = FuncThread(work)
        self._discord_thread.start()

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return
        self._tray = QSystemTrayIcon(_make_app_icon(), self)
        self._tray.setToolTip(APP_NAME)
        self._tray_menu = QMenu()   # keep a reference or Qt garbage-collects it
        self._tray_menu.addAction(f"Open {APP_NAME}", self._raise_window)
        self._tray_menu.addSeparator()
        self._tray_menu.addAction("Quit", QApplication.quit)
        self._tray.setContextMenu(self._tray_menu)
        self._tray.messageClicked.connect(self._raise_window)
        self._tray.activated.connect(
            lambda reason: self._raise_window()
            if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self._tray.show()

    def _raise_window(self) -> None:
        self.setWindowState((self.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        self.show()
        self.raise_()
        self.activateWindow()

    def _run_when_done_action(self) -> None:
        if self._when_done == "quit":
            QTimer.singleShot(1800, QApplication.quit)
        elif self._when_done == "sleep":
            try:
                QTimer.singleShot(1800, lambda: subprocess.Popen(
                    ["osascript", "-e", 'tell application "System Events" to sleep']))
            except Exception:
                pass

    def _deliver_outputs(self) -> None:
        """Copy this run's successful outputs to the delivery folder (Properties →
        Watch & Auto-render → Delivery). Runs on a background thread so multi-GB
        copies never block the UI; reports through the status bar + Live Logs."""
        dest_root = (self._deliver_dir or "").strip()
        if not dest_root:
            return
        outputs = [j.output_path for j in self._jobs
                   if j.status == "success" and j.output_path and Path(j.output_path).exists()]
        if not outputs:
            return

        def work(paths: list, dest: str) -> None:
            copied = 0
            try:
                Path(dest).mkdir(parents=True, exist_ok=True)
                for src in paths:
                    s = Path(src)
                    target = Path(dest) / s.name
                    if s.is_dir():     # image-sequence folder
                        shutil.copytree(s, target, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s, target)
                    copied += 1
                self._delivery_log.emit(
                    f"Delivered {copied} render(s) to {Path(dest).name}", "success")
            except Exception as exc:
                self._delivery_log.emit(f"Delivery copy failed: {exc}", "error")

        paths, dest = outputs, dest_root
        self._delivery_thread = FuncThread(lambda: work(paths, dest))
        self._delivery_thread.start()

    @staticmethod
    def _sheet_path_for(output: str) -> Path | None:
        """Where the contact sheet for a given output lives: next to a movie
        file, or inside an image-sequence folder."""
        if not output:
            return None
        p = Path(output)
        if p.suffix:
            return p.with_name(p.stem + "_contactsheet.png")
        return p / "_contactsheet.png"

    def _generate_contact_sheets(self) -> None:
        """Build a contact-sheet thumbnail grid for each successful output, on a
        managed background thread (ffmpeg can take a few seconds per output)."""
        targets: list[tuple[str, str]] = []
        for j in self._jobs:
            if j.status != "success" or not j.output_path:
                continue
            sheet = self._sheet_path_for(j.output_path)
            if sheet is None or sheet.exists():
                continue
            if Path(j.output_path).exists():
                targets.append((j.output_path, str(sheet)))
        if not targets:
            return

        def work(items: list) -> None:
            built = 0
            for src, dest in items:
                try:
                    if build_contact_sheet(src, dest):
                        built += 1
                except Exception:
                    pass
            self._sheets_built.emit(built)

        self._sheet_thread = FuncThread(lambda: work(targets))
        self._sheet_thread.start()

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
            try:
                html_path = REPORTS_DIR / f"run_report_{stamp}.html"
                html_path.write_text(self._build_html_report(), encoding="utf-8")
                self._last_html_report_path = str(html_path)
                if hasattr(self, "_open_html_action"):
                    self._open_html_action.setEnabled(True)
            except Exception:
                pass
        except Exception:
            pass

    def _build_html_report(self) -> str:
        """A standalone, shareable HTML report of the last run: per-job status,
        timing, sec/frame, error text, and embedded contact-sheet thumbnails."""
        import html as _html
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        scene = _html.escape(self.scene_panel.scene_edit.text().strip() or "—")
        rows = []
        for j in self._jobs:
            opts = j.render_options
            frames = f"{opts.frame_start}-{opts.frame_end}" if opts else ""
            dur = self._fmt_dur(self._job_durations.get(j.id, 0.0))
            m = self._job_metrics.get(j.id, {})
            spf = f"{m['avg_spf']:.1f}s" if m.get("avg_spf") else "—"
            _kwh, _cost = estimate_energy_cost(
                self._job_durations.get(j.id, 0.0), self._power_watts, self._power_rate)
            cost = f"${_cost:.2f}" if _cost else "—"
            color = {"success": "#3ba55d", "failed": "#ed4245",
                     "cancelled": "#888"}.get(j.status, "#bbb")
            sheet_html = ""
            sheet = self._sheet_path_for(j.output_path) if j.status == "success" else None
            if sheet is not None:
                try:
                    sheet_html = f'<div><img src="{_html.escape(sheet.as_uri())}" loading="lazy"></div>'
                except Exception:
                    sheet_html = ""
            err = f'<div class="err">{_html.escape(j.error)}</div>' if j.error else ""
            extra = f'<tr><td colspan="6">{sheet_html}{err}</td></tr>' if (sheet_html or err) else ""
            rows.append(
                f'<tr><td>{_html.escape(j.label or f"Job {j.id}")}</td>'
                f'<td style="color:{color};font-weight:600">{_html.escape(j.status)}</td>'
                f'<td>{_html.escape(frames)}</td><td>{_html.escape(dur)}</td>'
                f'<td>{_html.escape(spf)}</td><td>{_html.escape(cost)}</td></tr>{extra}')
        body = "\n".join(rows)
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<title>Render Report — {stamp}</title><style>'
            'body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#15171c;'
            'color:#e6e6e6;margin:0;padding:32px}'
            'h1{font-size:20px;margin:0 0 4px}.sub{color:#9aa0a6;font-size:13px;margin-bottom:24px}'
            'table{border-collapse:collapse;width:100%}'
            'th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #2a2d34;'
            'font-size:13px;vertical-align:top}'
            'th{color:#9aa0a6;font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.04em}'
            'img{max-width:100%;border-radius:6px;margin:8px 0}'
            '.err{color:#ed4245;font-family:ui-monospace,monospace;font-size:12px;'
            'white-space:pre-wrap;margin:6px 0}.brand{color:#e8833a;font-weight:700}'
            '</style></head><body>'
            '<h1><span class="brand">Render Mapper Pro</span> — Render Report</h1>'
            f'<div class="sub">{stamp} · Scene: {scene}</div>'
            '<table><thead><tr><th>Job</th><th>Status</th><th>Frames</th>'
            '<th>Duration</th><th>Avg/frame</th><th>Est. Cost</th></tr></thead>'
            f'<tbody>{body}</tbody></table></body></html>')

    def _open_last_report(self) -> None:
        if self._last_report_path and Path(self._last_report_path).exists():
            self._open_path(self._last_report_path)
        else:
            self._show_toast("No run report yet", "warning")

    def _open_html_report(self) -> None:
        if self._last_html_report_path and Path(self._last_html_report_path).exists():
            self._open_path(self._last_html_report_path)
        else:
            self._show_toast("No HTML report yet — run a render first.", "warning")

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
    def _command_actions(self) -> list[tuple[str, object]]:
        """The (label, callable) pairs offered by the command palette."""
        return [
            ("Scan Scene", self._scan_scene),
            ("Render Selected", lambda: self._start_render(render_all=False)),
            ("Render All", lambda: self._start_render(render_all=True)),
            ("Properties…", self._show_properties_dialog),
            ("Open Project…", self._open_project),
            ("Save Project As…", self._save_project),
            ("Save Preset…", self._save_preset),
            ("Load Preset…", self._load_preset),
            ("Render History…", self._show_history_dialog),
            ("Open HTML Render Report", self._open_html_report),
            ("Power & Cost Settings…", self._show_power_settings),
            ("Toggle Light/Dark Theme", lambda: self._toggle_theme(self._theme_mode != "light")),
            ("Quick Start", self._show_quick_start),
            ("Copy Diagnostics", self._copy_diagnostics),
            ("Check for Updates…", lambda: self._check_for_updates(manual=True)),
        ]

    def _show_command_palette(self) -> None:
        """A ⌘K / Ctrl+K fuzzy action launcher — type a few letters, Enter runs it."""
        actions = self._command_actions()
        by_name = dict(actions)
        dlg = QDialog(self)
        dlg.setWindowTitle("Command Palette")
        dlg.resize(460, 380)
        lay = QVBoxLayout(dlg)
        search = QLineEdit()
        search.setPlaceholderText("Type a command…")
        lay.addWidget(search)
        lst = QListWidget()
        lay.addWidget(lst)

        def populate(flt: str = "") -> None:
            lst.clear()
            f = flt.strip().lower()
            for name, _fn in actions:
                if all(tok in name.lower() for tok in f.split()):
                    lst.addItem(QListWidgetItem(name))
            if lst.count():
                lst.setCurrentRow(0)

        def run_current() -> None:
            it = lst.currentItem()
            if it is None:
                return
            fn = by_name.get(it.text())
            dlg.accept()
            if callable(fn):
                fn()

        # Down-arrow from the search box drops focus into the result list.
        def on_search_key(event):  # type: ignore[no-untyped-def]
            if event.key() in (Qt.Key_Down, Qt.Key_Up) and lst.count():
                lst.setFocus()
                return
            QLineEdit.keyPressEvent(search, event)

        search.keyPressEvent = on_search_key   # type: ignore[method-assign]
        search.textChanged.connect(populate)
        search.returnPressed.connect(run_current)
        lst.itemActivated.connect(lambda _i: run_current())
        populate()
        search.setFocus()
        dlg.exec()

    def _show_farm_nodes(self) -> None:
        """A live list of the Deadline farm's render nodes (reuses the proven
        deadlinecommand query path), with a Refresh button."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Farm Nodes")
        dlg.setMinimumSize(420, 460)
        lay = QVBoxLayout(dlg)
        status = QLabel("Fetching nodes…")
        status.setStyleSheet(f"color:{self._palette.text_muted}; font-size:11px;")
        lay.addWidget(status)
        lst = QListWidget()
        lay.addWidget(lst)
        roww = QHBoxLayout()
        refresh_btn = QPushButton("Refresh")
        roww.addWidget(refresh_btn)
        roww.addStretch()
        lay.addLayout(roww)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)

        def _on_done(res: dict) -> None:
            try:
                refresh_btn.setEnabled(True)
                if not res.get("ok"):
                    status.setText(f"Couldn't reach the farm: {(res.get('error', '') or '')[:200]}")
                    return
                machines = res.get("machines", [])
                lst.clear()
                for m in machines:
                    lst.addItem(QListWidgetItem(m))
                status.setText(f"{len(machines)} node(s) on the farm.")
            except RuntimeError:
                pass

        def fetch() -> None:
            cmd = self.deadline_panel.dl_cmd_edit.text().strip() \
                or find_deadlinecommand() or "deadlinecommand"
            if not Path(cmd).exists() and not shutil.which(cmd):
                status.setText("deadlinecommand not found — set it in Properties → Deadline.")
                return
            if self._farm_nodes_thread is not None and self._farm_nodes_thread.isRunning():
                return
            status.setText("Fetching nodes…")
            refresh_btn.setEnabled(False)
            self._farm_nodes_thread = DeadlineQueryThread(
                cmd, self.deadline_panel.dl_repo_edit.text().strip())
            self._farm_nodes_thread.result.connect(_on_done)
            self._farm_nodes_thread.start()

        refresh_btn.clicked.connect(fetch)
        fetch()
        dlg.exec()

    def _show_notification_settings(self) -> None:
        """Configure render-event notifications: system tray + an optional Discord
        webhook. Everything also always goes to Live Logs (no-popup-safe)."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Notifications")
        dlg.setMinimumWidth(440)
        form = QFormLayout(dlg)
        desktop_cb = QCheckBox("Desktop notifications (system tray)")
        desktop_cb.setChecked(self._notify_desktop)
        form.addRow(desktop_cb)
        webhook = QLineEdit(self._discord_webhook)
        webhook.setPlaceholderText("https://discord.com/api/webhooks/…")
        form.addRow("Discord webhook URL:", webhook)
        hint = QLabel("Render-complete and failure events post here. Leave blank to disable. "
                      "Create one in Discord → Server Settings → Integrations → Webhooks.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{self._palette.text_muted}; font-size:11px;")
        form.addRow(hint)
        test_btn = QPushButton("Send Test")

        def _send_test() -> None:
            self._discord_webhook = webhook.text().strip()
            if not self._discord_webhook:
                self._show_toast("Enter a webhook URL first.", "warning")
                return
            self._dispatch_discord("✅ Test notification from Render Mapper Pro")
            self._show_toast("Test sent — check Discord (and Live Logs for errors).", "info")

        test_btn.clicked.connect(_send_test)
        form.addRow(test_btn)
        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        form.addRow(bb)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        if dlg.exec():
            self._notify_desktop = desktop_cb.isChecked()
            self._discord_webhook = webhook.text().strip()
            self._schedule_save()

    def _show_power_settings(self) -> None:
        """Configure the per-machine power draw + electricity rate used to
        estimate render cost in the history and HTML report."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Power & Cost")
        form = QFormLayout(dlg)
        watts = QDoubleSpinBox()
        watts.setRange(0, 100000)
        watts.setDecimals(0)
        watts.setSuffix(" W")
        watts.setValue(self._power_watts)
        rate = QDoubleSpinBox()
        rate.setRange(0, 100)
        rate.setDecimals(3)
        rate.setPrefix("$ ")
        rate.setSuffix(" /kWh")
        rate.setValue(self._power_rate)
        form.addRow("Machine power draw:", watts)
        form.addRow("Electricity rate:", rate)
        hint = QLabel("Used to estimate render energy + cost in History and the HTML report.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color:{self._palette.text_muted}; font-size:11px;")
        form.addRow(hint)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        form.addRow(bb)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        if dlg.exec():
            self._power_watts = float(watts.value())
            self._power_rate = float(rate.value())
            self._schedule_save()

    def _show_image_dialog(self, path: str, title: str) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(960, 640)
        lay = QVBoxLayout(dlg)
        lbl = QLabel()
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pix = QPixmap(path)
        if pix.isNull():
            lbl.setText("Could not load image.")
        else:
            lbl.setPixmap(pix.scaled(920, 560, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(lbl)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def _show_history_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Render History")
        dlg.setMinimumSize(660, 420)
        lay = QVBoxLayout(dlg)
        table = QTableWidget(0, 7)
        table.setHorizontalHeaderLabels(
            ["When", "Job", "Status", "Duration", "s/frame", "Cost", "Output"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.horizontalHeader().setStretchLastSection(True)
        hist = self._load_history()
        for e in hist:
            r = table.rowCount()
            table.insertRow(r)
            table.setItem(r, 0, QTableWidgetItem(str(e.get("time", "")).replace("T", "  ")))
            table.setItem(r, 1, QTableWidgetItem(e.get("label", "")))
            table.setItem(r, 2, QTableWidgetItem(e.get("status", "")))
            table.setItem(r, 3, QTableWidgetItem(self._fmt_dur(float(e.get("duration", 0) or 0))))
            spf = float(e.get("avg_spf", 0) or 0)
            spf_item = QTableWidgetItem(f"{spf:.1f}s" if spf else "—")
            if e.get("p95_spf"):
                spf_item.setToolTip(f"p95: {float(e['p95_spf']):.1f}s/frame · "
                                    f"{int(e.get('frame_count', 0))} frames")
            table.setItem(r, 4, spf_item)
            cost = float(e.get("cost", 0) or 0)
            cost_item = QTableWidgetItem(f"${cost:.2f}" if cost else "—")
            if e.get("kwh"):
                cost_item.setToolTip(f"{float(e['kwh']):.2f} kWh")
            table.setItem(r, 5, cost_item)
            oi = QTableWidgetItem(e.get("output", ""))
            oi.setData(Qt.ItemDataRole.UserRole, e.get("output", ""))
            table.setItem(r, 6, oi)
        table.setColumnWidth(0, 160)
        table.setColumnWidth(1, 190)
        table.setColumnWidth(2, 70)
        table.setColumnWidth(3, 80)
        table.setColumnWidth(4, 70)
        table.setColumnWidth(5, 70)
        lay.addWidget(table)

        def sel_out() -> str:
            r = table.currentRow()
            it = table.item(r, 6) if r >= 0 else None
            return it.data(Qt.ItemDataRole.UserRole) if it else ""

        row = QHBoxLayout()
        reveal_b = QPushButton("Reveal Output")
        open_b = QPushButton("Open Output")
        sheet_b = QPushButton("Contact Sheet")
        clear_b = QPushButton("Clear History")
        def _do_reveal() -> None:
            o = sel_out()
            if o and Path(o).exists():
                reveal_in_file_manager(o)

        reveal_b.clicked.connect(_do_reveal)
        open_b.clicked.connect(lambda: self._open_path(sel_out()) if sel_out() else None)

        def show_sheet() -> None:
            out = sel_out()
            sheet = self._sheet_path_for(out)
            if sheet is not None and sheet.exists():
                self._show_image_dialog(str(sheet), "Contact Sheet")
                return
            if not out or not Path(out).exists():
                self._show_toast("Output not found.", "warning")
                return
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                ok = build_contact_sheet(out, str(sheet)) if sheet else False
            finally:
                QApplication.restoreOverrideCursor()
            if ok and sheet is not None:
                self._show_image_dialog(str(sheet), "Contact Sheet")
            else:
                self._show_toast("Couldn't build a contact sheet for this output.", "warning")

        sheet_b.clicked.connect(show_sheet)

        def do_clear() -> None:
            try:
                HISTORY_PATH.write_text("[]")
            except Exception:
                pass
            table.setRowCount(0)

        clear_b.clicked.connect(do_clear)
        row.addWidget(reveal_b)
        row.addWidget(open_b)
        row.addWidget(sheet_b)
        row.addStretch()
        row.addWidget(clear_b)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
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

    def _set_deadline_status(self, text: str, color: str) -> None:
        lbl = self.deadline_panel.connection_status_lbl
        lbl.setText(text)
        lbl.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _test_deadline_connection(self, interactive: bool = True) -> None:
        """Query the farm on a worker thread (never blocks the UI). When
        ``interactive``, show a result dialog; otherwise just update the panel
        status and, on failure, open Properties → Deadline to fix it."""
        if self._deadline_test_thread is not None and self._deadline_test_thread.isRunning():
            return
        deadline_cmd = self.deadline_panel.dl_cmd_edit.text().strip() \
            or find_deadlinecommand() or "deadlinecommand"
        if not Path(deadline_cmd).exists() and not shutil.which(deadline_cmd):
            self._set_deadline_status("✗ deadlinecommand not found", self._palette.danger)
            if interactive:
                QMessageBox.critical(
                    self, "Deadline Not Found",
                    "The Deadline client isn't installed on this machine (deadlinecommand "
                    "was not found).\n\nInstall the Thinkbox Deadline Client, or set its "
                    "path in Properties → Deadline → Command Path.")
            return

        self._append_log(f"[deadline] Testing connection using: {deadline_cmd}")
        self._set_deadline_status("… Testing connection", self._palette.warning)
        self._deadline_test_thread = DeadlineQueryThread(
            deadline_cmd, self.deadline_panel.dl_repo_edit.text().strip())
        self._deadline_test_thread.result.connect(
            lambda res, inter=interactive: self._on_deadline_test_done(res, inter))
        self._deadline_test_thread.start()

    def _on_deadline_test_done(self, res: dict, interactive: bool) -> None:
        if not res.get("ok"):
            self._set_deadline_status("✗ Couldn't reach the repository", self._palette.danger)
            self._append_log(f"[deadline] Connection failed: {res.get('error', '')}")
            if interactive:
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Icon.Warning)
                box.setWindowTitle("Deadline Unreachable")
                box.setText("Couldn't reach the Deadline repository.")
                box.setInformativeText(
                    "Check the Repository Path in Properties → Deadline, and that this "
                    "machine is on the studio network (or VPN). Details are in Live Logs.")
                box.setDetailedText(res.get("error", ""))
                box.exec()
            else:
                self._append_log("[app] Deadline not ready — opening Deadline settings.")
                self._show_properties_dialog(initial_tab="Deadline")
            return

        pools, groups, machines = res["pools"], res["groups"], res["machines"]
        dp = self.deadline_panel
        current_pool = dp.dl_pool_combo.currentText()
        current_sec = dp.dl_sec_pool_combo.currentText()
        dp.dl_pool_combo.clear()
        dp.dl_pool_combo.addItems(pools)
        dp.dl_sec_pool_combo.clear()
        dp.dl_sec_pool_combo.addItems([""] + pools)
        if current_pool:
            dp.dl_pool_combo.setCurrentText(current_pool)
        if current_sec:
            dp.dl_sec_pool_combo.setCurrentText(current_sec)
        if groups:
            current_group = dp.dl_group_combo.currentText()
            dp.dl_group_combo.clear()
            dp.dl_group_combo.addItems([""] + groups)
            if current_group:
                dp.dl_group_combo.setCurrentText(current_group)
        if machines:
            dp.dl_machines_list.blockSignals(True)
            checked = {dp.dl_machines_list.item(i).text().strip()
                       for i in range(dp.dl_machines_list.count())
                       if dp.dl_machines_list.item(i).checkState() == Qt.CheckState.Checked}
            dp.dl_machines_list.clear()
            for m in machines:
                item = QListWidgetItem(m)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked if (m in checked or not checked) else Qt.CheckState.Unchecked)
                dp.dl_machines_list.addItem(item)
            dp.dl_machines_list.blockSignals(False)

        self._set_deadline_status(
            f"✓ Connected — {len(pools)} pools · {len(groups)} groups · {len(machines)} machines",
            self._palette.success)
        self._append_log(f"[deadline] Connected: {len(pools)} pools, {len(groups)} groups, "
                         f"{len(machines)} machines.")
        self._show_toast("Deadline connected", "success")
        if interactive:
            QMessageBox.information(
                self, "Deadline Connected",
                f"Connected to the repository.\nPools, groups and the machine list were "
                f"updated ({len(machines)} machines).")

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
                        name = f"Render Mapper Pro Job - {scene_path.name}"
                else:
                    name = f"Render Mapper Pro Job - {scene_path.name}"
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
        preset_dict: dict | None = None
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
        if ans != QMessageBox.StandardButton.Yes:
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
        layout_state = bytes(self.saveState().toBase64().data()).decode("ascii")
        layout_geometry = bytes(self.saveGeometry().toBase64().data()).decode("ascii")

        def _opts_dict(opts: RenderOptions | None) -> dict | None:
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
            "power_watts": self._power_watts,
            "power_rate": self._power_rate,
            "notify_desktop": self._notify_desktop,
            "discord_webhook": self._discord_webhook,
            "live_preview": self._preview_enabled,
            "watch_folder": watch_folder,
            "watch_enabled": watch_enabled,
            "watch_interval_ms": watch_interval_ms,
            "watch_settle": watch_settle,
            "autorender_enabled": self._autorender_enabled,
            "autorender_start": self._autorender_start,
            "autorender_output": self._autorender_output,
            "autorender_pattern": self._autorender_pattern,
            "deliver_dir": self._deliver_dir,
            "render_targets": self.scene_panel.get_targets(),
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
            "engine": self.render_panel.engine_value(),
            "output_profile": self.render_panel.profile_combo.currentText(),
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

    def _migrate_profile(self, d: dict) -> dict:
        """Bring an older saved profile up to the current schema. A newer-than-
        current profile is loaded as-is (best effort) so a downgrade never wipes
        state. This is the scaffold to hang real field migrations on."""
        try:
            ver = int(d.get("version", 1))
        except (TypeError, ValueError):
            ver = 1
        if ver > PROFILE_VERSION:
            self._append_log(
                f"[app] Settings are from a newer build (v{ver} > v{PROFILE_VERSION}); "
                "loading what's compatible.")
            return d
        if ver == PROFILE_VERSION:
            return d
        migrated = dict(d)
        # Forward migrations go here, smallest version first, e.g.:
        #   if ver < 4: migrated = self._migrate_v3_to_v4(migrated)
        migrated["version"] = PROFILE_VERSION
        self._append_log(f"[app] Migrated settings from v{ver} to v{PROFILE_VERSION}.")
        return migrated

    def _apply_profile_data(self, d: dict) -> None:
        d = self._migrate_profile(d)
        # Accent stays the brand orange; honor the saved light/dark choice.
        tm = str(d.get("theme_mode", self._theme_mode))
        if tm in ("dark", "light"):
            self._theme_mode = tm
        try:
            self._power_watts = float(d.get("power_watts", self._power_watts))
            self._power_rate = float(d.get("power_rate", self._power_rate))
        except (TypeError, ValueError):
            pass
        self._notify_desktop = bool(d.get("notify_desktop", self._notify_desktop))
        self._discord_webhook = str(d.get("discord_webhook", self._discord_webhook) or "")
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
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
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
        self._deadline_job_name_template = str(d.get("deadline_job_name_template", "Render Mapper Pro Job - {scene_name}"))
        if self._deadline_job_name_template == "BlenderRender Job - {scene_name}":   # migrate old app name
            self._deadline_job_name_template = "Render Mapper Pro Job - {scene_name}"
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
        self.render_panel.set_engine_value(eng)
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
        unresolved: list[str] = []
        seen_missing: set[str] = set()

        def note_missing(vp: str, vn: str) -> None:
            name = vn or Path(vp).name or vp
            if name and name.lower() not in seen_missing:
                seen_missing.add(name.lower())
                unresolved.append(name)

        for vp, vn in video_entries:
            resolved = resolve_video(vp, vn)
            if resolved:
                if resolved not in vids:
                    vids.append(resolved)
            else:
                note_missing(vp, vn)

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
            elif mn and not resolved:
                note_missing(vp, vn)

        self.scene_panel.set_videos(vids)
        self.scene_panel.set_assignments(asn)
        if unresolved:
            for name in unresolved:
                self._append_log(f"[load] Clip not found — skipped: {name}")
            n = len(unresolved)
            self._show_toast(
                f"{n} clip{'s' if n != 1 else ''} from the project couldn't be located "
                f"and {'were' if n != 1 else 'was'} skipped — see Live Logs.",
                "warning")

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
        self._deliver_dir = str(d.get("deliver_dir", "") or "")
        tg = d.get("render_targets", [])
        if isinstance(tg, list):
            self.scene_panel.set_targets(tg)
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
                    ro: RenderOptions | None = None
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
                        deadline_job_name_template=str(jd.get("deadline_job_name_template", "Render Mapper Pro Job - {scene_name}")),
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
        # Widgets were built under the default dark theme; apply a saved light
        # choice now and sync the menu checkbox without re-triggering a restyle.
        if self._theme_mode != "dark":
            self._restyle_all()
        if hasattr(self, "theme_action"):
            self.theme_action.blockSignals(True)
            self.theme_action.setChecked(self._theme_mode == "light")
            self.theme_action.blockSignals(False)

    def _maybe_first_run(self) -> None:
        """A one-time welcome on the very first launch: show what's detected and
        point new users at setup + Quick Start."""
        if not getattr(self, "_is_first_run", False):
            return
        blender = _find_blender(self._blender_path)
        ffmpeg = find_ffmpeg_tool("ffmpeg")
        lines = [
            f"Blender:  {'✓ ' + Path(blender).name if blender else '✗ not found — set it in Properties'}",
            f"Cinema 4D:  {'✓ detected' if self._c4dpy_path else '— not detected (optional)'}",
            f"ffmpeg:  {'✓ bundled' if ffmpeg else '✗ not found — frame extraction will be limited'}",
        ]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(f"Welcome to {APP_NAME}")
        box.setText("You're set up to map videos onto a 3D scene and render headlessly.")
        box.setInformativeText(
            "\n".join(lines)
            + "\n\nffmpeg ships with the app (used to extract clip frames). "
            "Drop in a scene, add clips, map them, and hit render.")
        qs = box.addButton("Open Quick Start", QMessageBox.ButtonRole.ActionRole)
        loc = box.addButton("Locate Blender…", QMessageBox.ButtonRole.ActionRole) if not blender else None
        box.addButton("Get Started", QMessageBox.ButtonRole.AcceptRole)
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
            data = json.dumps(self._profile_dict(), indent=2)
            # Atomic write: a crash/power-loss/full-disk mid-write must never
            # corrupt the existing profile. Write to a sibling temp file, fsync,
            # then atomically rename over the target (POSIX rename / NTFS replace).
            tmp = PROFILE_PATH.with_name(PROFILE_PATH.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(PROFILE_PATH)
        except Exception as exc:
            self._append_log(f"[app] Could not save settings: {exc}")

    def _set_when_done(self, value: str) -> None:
        self._when_done = value
        self._schedule_save()

    def _save_and_refresh_status(self) -> None:
        self._save_profile()
        self._update_status_bar()

    def _schedule_save(self) -> None:
        if self._save_timer is None:
            self._save_timer = QTimer(self)
            self._save_timer.setSingleShot(True)
            self._save_timer.timeout.connect(self._save_profile)
        self._save_timer.start(400)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._shutting_down = True   # late background signals become no-ops
        self._save_profile()
        if self._render_thread and self._render_thread.isRunning():
            self._render_thread.request_cancel()
            self._render_thread.wait(3000)
        # Cancel + wait on the other workers too, so a QThread isn't destroyed
        # mid-run (which crashes Qt) and its headless subprocess isn't orphaned.
        for attr in ("_preview_thread", "_discovery_thread", "_export_thread",
                     "_runtime_install_thread", "_deadline_test_thread",
                     "_props_deadline_thread", "_farm_nodes_thread", "_update_check_thread"):
            t = getattr(self, attr, None)
            if t is not None and t.isRunning():
                if hasattr(t, "request_cancel"):
                    t.request_cancel()
                t.wait(3000)
        # A delivery copy can be many GB; wait longer so we never abort it
        # mid-copy and leave partial files in the delivery folder.
        dt = self._delivery_thread
        if dt is not None and dt.isRunning():
            dt.wait(30000)
        st = self._sheet_thread
        if st is not None and st.isRunning():
            st.wait(15000)
        nt = self._discord_thread
        if nt is not None and nt.isRunning():
            nt.wait(5000)
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

        objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc") or "")
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
        # Crisp scaling on fractional-DPI displays (e.g. Windows 150%, 4K). Must
        # be set before the QApplication is constructed.
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        app = QApplication(sys.argv)
    assert isinstance(app, QApplication)
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
        if probe.state() == QLocalSocket.LocalSocketState.ConnectedState:
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
        win.setWindowState((win.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        win.show()
        win.raise_()
        win.activateWindow()

    server.newConnection.connect(_on_second_launch)

    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    run_qt_app()
