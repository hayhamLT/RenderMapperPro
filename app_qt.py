from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

# A frozen build ships its own Python with no CA certificates, so every HTTPS
# request (the update check, the managed-Blender download, …) fails verification
# with CERTIFICATE_VERIFY_FAILED — which surfaces as "Couldn't reach GitHub".
# Point OpenSSL at certifi's bundled cacert.pem before any network call. (From
# source the system certs are used, so this only matters in the .app/.exe.)
if getattr(sys, "frozen", False):
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    except Exception:
        pass

from PySide6.QtCore import (
    QByteArray,
    QEvent,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
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
    QVBoxLayout,
    QWidget,
)

import app_version
import icons
import theme as T
from app_window.deadline_mixin import DeadlineMixin
from app_window.preset_mixin import PRESETS_DIR, PresetMixin
from app_window.queue_mixin import QueueMixin
from app_window.reporting_mixin import ReportMixin
from app_window.runtime_mixin import RuntimeMixin
from app_window.update_mixin import UpdateMixin
from core.asset_grouping import GroupingConfig as AssetGroupingConfig
from core.asset_grouping import group_clips, parse_clip
from core.jobs import disk_space_warnings, migrate_profile
from core.logging_setup import get_logger
from core.metrics import (
    auto_chunk_size,
    estimate_energy_cost,
    predict_total_seconds,
)
from core.models import (
    VIDEO_MAPPING_MODE_EMISSION,
    JobConfig,
    MaterialVideoAssignment,
    RenderJob,
    RenderOptions,
    is_c4d_scene,
    is_web_scene,
    uses_web_backend,
)
from core.reporting import format_duration, friendly_error_hint
from core.runtime import (
    _managed_blender_executable,
    _norm_blender,
    _runtime_download_spec,
)
from core.utils import (
    IMAGE_MEDIA_EXTENSIONS,
    OUTPUT_PROFILES,
    VIDEO_EXTENSIONS,
    atomic_write_text,
    ext_for_format,
    file_exists,
    subprocess_creation_flags,
)
from core.utils import ssl_context as _ssl_context
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
HISTORY_PATH = Path.home() / ".blender_video_mapper" / "history.json"
# Branded file extensions (JSON underneath) for user-facing Save/Open.
PROJECT_EXT = ".rmproj"      # full project: scene, clips, mappings, queue
LOG_PATH = Path.home() / ".blender_video_mapper" / "logs" / "app_qt.log"
APP_NAME = app_version.APP_NAME
APP_VERSION: str = app_version.__version__  # single source of truth (see app_version.py)
PROFILE_VERSION = 3
LOG_MAX_BYTES = 2 * 1024 * 1024  # 2 MB

_log = get_logger(__name__)


def _make_app_icon() -> QIcon:
    return icons.app_icon()


# Version tag for QMainWindow.saveState()/restoreState(). A saved dock layout is
# only restored when its version matches — so when the set of docks changes
# (panel added/renamed/removed), bump this and stale layouts are cleanly ignored
# (we fall back to the default preset) instead of restoring into a broken state.
LAYOUT_STATE_VERSION = 2


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


class BlenderVideoMapperQt(QMainWindow, QueueMixin, PresetMixin, DeadlineMixin, UpdateMixin, RuntimeMixin,
                          ReportMixin):
    _update_checked = Signal(object, bool, str)   # (manifest dict | None, was-manual, error-text)
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
        self._runtime_install_thread = None   # set by RuntimeMixin; typed in app_window/base.py
        self._runtime_progress_dialog = None
        self._runtime_prompted = False
        self._save_timer: QTimer | None = None

        # "system" follows the OS light/dark appearance; "light"/"dark" pin it.
        # New installs follow the OS; a saved explicit choice is honoured on load.
        self._theme_mode = "system"
        self._accent = T.ACCENT_ORANGE
        self._palette: T.Palette = T.build_palette(self._effective_theme_mode(), self._accent)
        # Re-theme live when the OS appearance flips, but only while following it.
        _app = QApplication.instance()
        if isinstance(_app, QApplication):
            _app.styleHints().colorSchemeChanged.connect(self._on_os_color_scheme_changed)

        self._preview_enabled = True
        self._preview_path = ""
        self._preview_timer: QTimer | None = None
        self._last_preview_sig: tuple | None = None   # auto-preview re-renders only when this changes
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
        self._asset_grouping = AssetGroupingConfig()   # filename-convention previz assembly
        self._asset_group_jobs: dict = {}   # (setup, asset) → job id, for grouped-watch dedup
        self._restore_session_on_launch = False   # False = open clean; True = reopen last scene+queue
        self._last_session: dict = {}      # snapshot of the previous session, for Reopen Last Session
        self._material_aspects: dict = {}   # material → screen aspect (from discovery)
        self._aspect_warned: set = set()    # (material, clip) pairs already warned about
        # Render-preflight inputs from the last scan (None = unknown).
        self._scene_has_lighting: bool | None = None       # Blender: any lamp/world light?
        self._redshift_materials: set | None = None        # C4D: materials that take a clip
        self._scanned_scene: str = ""                      # the scene those two describe
        self._autorender_start = False        # auto-start vs queue-only
        self._last_report_path = ""
        self._last_html_report_path = ""
        self._c4d_force_submit = False   # override the C4D blank-bake guard
        self._single_instance_server: object = None   # set by run_qt_app, kept alive
        self._power_watts = 300.0        # est. machine draw for cost reporting
        self._power_rate = 0.15          # electricity rate ($/kWh)
        self._notify_desktop = True      # system-tray notifications on render events
        self._discord_webhook = ""       # optional Discord webhook for render events
        self._check_updates_on_launch = True   # check GitHub for a newer release on startup (on by default)
        self._skipped_update = ""              # a release tag the user chose to skip (no more launch nags)
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
        self._apply_accessible_names()
        self._load_profile()
        self._update_status_bar()
        # Check for a newer release a few seconds after launch (if the user hasn't
        # turned the on-launch check off). A newer version pops the offer dialog.
        QTimer.singleShot(3000, self._launch_update_check)
        QTimer.singleShot(300, self._maybe_first_run)   # one-time welcome on first launch
        # Size/position the window once it's shown: restore the user's last
        # adjustment if it's reasonable, otherwise default to 70% of the screen
        # centered.
        QTimer.singleShot(0, self._init_window_geometry)
        QTimer.singleShot(250, self._init_blender)

    def _effective_theme_mode(self) -> str:
        """The concrete 'light'/'dark' to paint — resolving 'system' against the
        OS appearance."""
        if self._theme_mode == "system":
            return T.resolve_system_mode()
        return self._theme_mode

    def _on_os_color_scheme_changed(self, _scheme: object = None) -> None:
        """Re-theme when the OS flips light/dark — only while following system."""
        if self._theme_mode == "system":
            self._restyle_all()
            self._sync_theme_menu()

    def _sync_theme_menu(self) -> None:
        """Reflect the current mode in the View-menu theme actions."""
        pairs = (
            (getattr(self, "system_theme_action", None), self._theme_mode == "system"),
            (getattr(self, "theme_action", None), self._effective_theme_mode() == "light"),
        )
        for act, on in pairs:
            if act is not None:
                act.blockSignals(True)
                act.setChecked(on)
                act.blockSignals(False)

    def _follow_system_theme(self, on: bool) -> None:
        # Turning it off freezes the current appearance as an explicit choice.
        self._theme_mode = "system" if on else self._effective_theme_mode()
        self._restyle_all()
        self._sync_theme_menu()
        self._schedule_save()

    def _apply_theme(self) -> None:
        self._palette = T.build_palette(self._effective_theme_mode(), self._accent)
        set_active_palette(self._palette)
        qss = T.stylesheet(self._palette)
        # Apply the theme app-wide, not just to the main window: a window-only
        # stylesheet leaves top-level dialogs (Properties, About, …) unpainted —
        # the `QWidget { background: transparent }` rule makes the dialog itself
        # transparent (→ black) while only its children get themed. An app-level
        # sheet styles each dialog directly, so its background matches the app.
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setStyleSheet(qss)
        self.setStyleSheet(qss)

    def _toggle_theme(self, light: bool) -> None:
        # Picking an explicit light/dark leaves system-follow mode.
        mode = "light" if light else "dark"
        if mode == self._theme_mode:
            return
        self._theme_mode = mode
        self._restyle_all()
        self._sync_theme_menu()
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
        new_act = profile.addAction("New", lambda: self._new_session(confirm=True))
        new_act.setShortcut(QKeySequence.StandardKey.New)           # ⌘N / Ctrl+N
        profile.addAction("Reopen Last Session", self._reopen_last_session)
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
        self.system_theme_action = QAction("Follow System Appearance", self, checkable=True)
        self.system_theme_action.setChecked(self._theme_mode == "system")
        self.system_theme_action.toggled.connect(self._follow_system_theme)
        self.view_menu.addAction(self.system_theme_action)
        self.theme_action = QAction("Light Theme", self, checkable=True)
        self.theme_action.setChecked(self._effective_theme_mode() == "light")
        self.theme_action.toggled.connect(self._toggle_theme)
        self.view_menu.addAction(self.theme_action)
        self.preview_action = QAction("Live Preview While Rendering", self, checkable=True)
        self.preview_action.setChecked(self._preview_enabled)
        self.preview_action.toggled.connect(self._set_preview_enabled)
        self.view_menu.addAction(self.preview_action)

        help_menu = mb.addMenu("Help")
        help_menu.addAction("User Guide", self._show_user_guide)
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
        """A themed rich-text help dialog. Links are click-through: ``http(s)``
        anchors open in the system browser, and ``action:<name>`` anchors run an
        in-app action (e.g. ``action:properties/Updates`` opens that settings
        tab) so the guides are interactive, not just text."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.setMinimumSize(600, 540)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        browser = QTextBrowser()
        browser.setOpenLinks(False)   # route every click through _on_help_anchor
        pal = self._palette
        browser.setStyleSheet(
            f"QTextBrowser {{ border: none; background: {pal.surface}; padding: 22px 24px; }}")
        browser.setHtml(html)
        browser.anchorClicked.connect(lambda url: self._on_help_anchor(url, dlg))
        lay.addWidget(browser)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.exec()

    def _on_help_anchor(self, url: QUrl, _dlg: QDialog) -> None:
        spec = url.toString()
        if not spec.startswith("action:"):
            QDesktopServices.openUrl(url)   # external link → system browser
            return
        # Every target is a settings/info dialog that opens *over* the help
        # window and returns to it, so the guide stays open for more reading.
        name, _, arg = spec[len("action:"):].partition("/")
        actions = {
            "properties": lambda: self._show_properties_dialog(arg or None),
            "shortcuts": self._show_shortcuts_help,
            "history": self._show_history_dialog,
            "power": self._show_power_settings,
            "notifications": self._show_notification_settings,
        }
        if name in actions:
            actions[name]()

    def _help_css(self) -> str:
        p = self._palette
        return (
            f"<style>"
            f"body{{color:{p.text}; font-size:13px; line-height:1.55;}}"
            f"h2{{color:{p.accent}; font-size:18px; margin:0 0 2px;}}"
            f"h3{{color:{p.text}; font-size:12px; font-weight:700; "
            f"letter-spacing:.4px; margin:20px 0 6px; text-transform:uppercase;}}"
            f"p{{margin:6px 0;}}"
            f".lead{{color:{p.text_muted}; font-size:13px; margin:2px 0 16px;}}"
            f".muted{{color:{p.text_muted};}}"
            f"a{{color:{p.info}; font-weight:600;}}"
            f"code,kbd{{background:{p.surface_alt}; color:{p.text}; padding:1px 6px; "
            f"border-radius:4px; font-family:Menlo,monospace; font-size:12px;}}"
            f"table{{border-collapse:collapse; margin:2px 0;}}"
            f"td{{padding:6px 12px 6px 0; vertical-align:top;}}"
            f"td.num{{background:{p.accent}; color:#ffffff; font-weight:700; "
            f"text-align:center; padding:6px 0; margin:0;}}"
            f"td.fname{{color:{p.accent}; font-weight:700; white-space:nowrap; padding-right:16px;}}"
            f"</style>"
        )

    def _show_user_guide(self) -> None:
        """A tabbed, click-through User Guide — one tab per panel/area. The
        ``action:`` links jump straight to the relevant settings dialog, so the
        guide is interactive, not just a wall of text."""
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{APP_NAME} — User Guide")
        dlg.setMinimumSize(800, 660)
        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        pal = self._palette
        css = self._help_css()
        for title, body in self._guide_sections():
            browser = QTextBrowser()
            browser.setOpenLinks(False)
            browser.setStyleSheet(
                f"QTextBrowser {{ border: none; background: {pal.surface}; padding: 20px 26px; }}")
            browser.setHtml(css + body)
            browser.anchorClicked.connect(lambda url, d=dlg: self._on_help_anchor(url, d))
            tabs.addTab(browser, title.replace("&", "&&"))   # && → literal & (not a mnemonic)
        lay.addWidget(tabs)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.setContentsMargins(10, 8, 10, 8)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        dlg.exec()

    @staticmethod
    def _guide_sections() -> list[tuple[str, str]]:
        """(tab title, HTML body) for each User Guide page. Static HTML — the
        only dynamic bits are ``action:`` links, resolved by _on_help_anchor."""
        return [
            ("Getting Started", """
        <h2>Welcome</h2>
        <p class="lead">Render Mapper Pro maps your videos onto a 3D scene's
        materials and renders them — on your machine or a render farm. Each tab
        above covers one part of the app; here's the whole flow first.</p>
        <table class="steps" width="100%">
          <tr><td class="num" width="30">1</td><td><b>Add a scene</b> — drag a 3D
              file onto the <b>Scene</b> box, then click <b>Scan Scene</b>. See the
              <b>Scene &amp; Clips</b> tab.</td></tr>
          <tr><td class="num">2</td><td><b>Add clips</b> — drag your videos into the
              Videos list and pick a <b>Camera</b>.</td></tr>
          <tr><td class="num">3</td><td><b>Link them</b> — click <b>Auto-map</b> to
              match clips to materials by name, or link a selected pair by hand.</td></tr>
          <tr><td class="num">4</td><td><b>Set the output</b> — see the <b>Render
              Settings</b> tab for resolution, range, format and quality.</td></tr>
          <tr><td class="num">5</td><td><b>Queue &amp; render</b> — press <b>Start</b>
              (<kbd>⌘R</kbd>) and watch the <b>Live Preview</b>, or send it to a
              <b>Render Farm</b>.</td></tr>
        </table>
        <h3>The workspace</h3>
        <p class="muted">Panels float and dock freely — drag a panel's tab to
        rearrange, tab, or float it, pick a preset from <i>View → Layout</i>, and
        show/hide panels from <i>View</i>. Press <kbd>⌘K</kbd> for the command
        palette, and switch light/dark in <i>View</i>. First, point the app at
        Blender (or let it fetch one) in <a href="action:properties/Render Engines">Properties</a>.</p>
        """),
            ("Scene & Clips", """
        <h2>Scene &amp; Clips</h2>
        <p class="lead">Load a 3D scene, add your videos, and connect each clip to
        the material (screen) it should play on.</p>
        <h3>Loading a scene</h3>
        <p class="muted"><b>Drag &amp; drop</b> a file onto the Scene box, click
        <b>Browse</b>, or pick from <b>recent</b> (the ▾ button). Supported:
        Blender <code>.blend</code> / <code>.fbx</code> / <code>.obj</code> /
        <code>.usd</code> / <code>.abc</code>, Cinema&nbsp;4D <code>.c4d</code>, and
        glTF <code>.glb</code> / <code>.gltf</code>. Then <b>Scan Scene</b> reads
        its materials + cameras and pulls the scene's own render settings (fps,
        range, resolution, engine) into the UI.</p>
        <h3>Materials &amp; videos</h3>
        <table class="feat" width="100%">
          <tr><td class="fname">Filter</td><td>Type in the search boxes to narrow long
              material or video lists.</td></tr>
          <tr><td class="fname">Add clips</td><td>Drag videos in or click <b>Add</b>.
              A clip with sound shows a <b>speaker</b> badge — click it to mute that
              clip (the rest are mixed into the render).</td></tr>
          <tr><td class="fname">Render target</td><td>Right-click a material →
              <b>Mark as Render Target</b> (or click the stripe on its left) to flag
              the screens an auto-render must cover.</td></tr>
        </table>
        <h3>Linking a clip to a material</h3>
        <p class="muted"><b>Auto-map</b> is easiest: it links by name, so a
        <code>Screen</code> material grabs <code>Screen_v3.mp4</code> — it only fills
        empty materials and never overwrites a manual link. To link by hand, select
        one material + one video and click the <b>link</b> button between the lists.
        A colored <b>stripe</b> marks each pair; hover or select either side to light
        up its partner.</p>
        <h3>Watch folder</h3>
        <p class="muted">Turn on the <b>watch folder</b> (clock button under the
        clips) and any clip dropped there imports + maps itself. It's version-aware
        — <code>Screen_v1</code>/<code>v2</code>/<code>_3</code> are one clip and the
        <b>latest wins</b>. Tune it in <a href="action:properties/Watch">Properties →
        Watch &amp; Auto-render</a>.</p>
        """),
            ("Render Settings", """
        <h2>Render Settings</h2>
        <p class="lead">Resolution, range, renderer, output, and the quality knobs —
        most are pulled from the scene on Scan, then yours to tweak.</p>
        <h3>Basics</h3>
        <table class="feat" width="100%">
          <tr><td class="fname">Resolution / FPS</td><td>Output width × height and
              frame rate.</td></tr>
          <tr><td class="fname">Frame range</td><td><b>Start</b>, <b>End</b> and
              <b>Step</b> (render every Nth frame).</td></tr>
          <tr><td class="fname">Renderer</td><td>Auto-picked by scene type —
              <b>Cycles</b>/<b>EEVEE</b> for Blender, <b>Redshift</b> for .c4d,
              <b>three.js</b> for .glb. Set the engine paths in
              <a href="action:properties/Render Engines">Properties → Render Engines</a>.</td></tr>
          <tr><td class="fname">Output</td><td>Profile — <b>H.264 MP4</b> (review),
              <b>ProRes MOV</b> (editorial), or <b>PNG/EXR sequence</b> (comp). The
              <b>Output path</b> accepts tokens: <code>{scene}</code>
              <code>{camera}</code> <code>{date}</code>.</td></tr>
        </table>
        <h3>Quality &amp; output</h3>
        <p class="muted">The quality &amp; output settings sit inline and adapt to the
        active renderer, so every control you see is real for that engine:</p>
        <table class="feat" width="100%">
          <tr><td class="fname">Blender</td><td>Samples, denoise, device (CPU/GPU),
              colour transform / exposure / gamma, transparent film.</td></tr>
          <tr><td class="fname">Redshift</td><td>Speed Preset (Draft→Final), Max/Min
              samples, adaptive Noise Threshold, denoise, GI bounces, Max Ray Depth.</td></tr>
          <tr><td class="fname">three.js</td><td>Lighting preset (auto/studio/outdoor/
              flat), intensity, and whether to respect the file's own lights.</td></tr>
        </table>
        <p class="muted">Plus a render <b>scale&nbsp;%</b> and an optional
        <b>burn-in</b> overlay (clip/version/frame/camera/date on every frame).</p>
        <h3>Presets — reuse a recipe</h3>
        <p class="muted">Dialled in a look you like? Save the whole render recipe
        (resolution, range, engine, quality, colour…) as a named <b>Preset</b> from
        the <b>Presets</b> panel (or <i>Profile → Save Preset…</i>). Later, pick it
        to <b>apply</b> it to the current settings — or to selected/checked queue
        jobs — so a new scene matches your house style in one click. Presets store
        <i>settings only</i>, never your scene or clips.</p>
        <h3>Output path &amp; tokens</h3>
        <p class="muted">The <b>Output path</b> takes tokens that fill in per job —
        click the <b>▾</b> beside it for the full menu: <code>{video}</code>
        <code>{scene}</code> <code>{camera}</code> <code>{res}</code>
        <code>{width}</code> <code>{height}</code> <code>{fps}</code>
        <code>{date}</code>.</p>
        """),
            ("Live Preview", """
        <h2>Live Preview</h2>
        <p class="lead">Render a single frame from your current settings — no queue
        needed — to check framing and mapping before committing to a full render.</p>
        <table class="feat" width="100%">
          <tr><td class="fname">Frame picker</td><td>Choose a frame with the
              <b>scrubber</b> (prev/next, slider, frame field).</td></tr>
          <tr><td class="fname">Render</td><td>Click the <b>camera</b> button to
              render that frame. A thin <b>progress bar</b> under the image shows it
              working.</td></tr>
          <tr><td class="fname">Scale</td><td><b>Full / ½ / ¼ / ⅛</b> — render at a
              fraction for a fast preview on heavy scenes.</td></tr>
          <tr><td class="fname">Auto</td><td>Re-renders the preview whenever you
              change the camera, resolution, frame or mapping (debounced).</td></tr>
          <tr><td class="fname">Zoom &amp; pan</td><td><b>Double-click</b> toggles
              Fit ⇄ 100% (centred on the click); <b>grab to pan</b> at 100%.</td></tr>
          <tr><td class="fname">Playback</td><td>After a full render the finished
              movie plays (looped) here — <kbd>Space</kbd> play/pauses.</td></tr>
        </table>
        """),
            ("Queue & Render", """
        <h2>Queue &amp; Render</h2>
        <p class="lead">The moment you map a clip it becomes a live job. Tick the
        ones to render, press Start, and track them here.</p>
        <h3>The columns</h3>
        <table class="feat" width="100%">
          <tr><td class="fname">Run</td><td>Checked jobs render when you press Start.</td></tr>
          <tr><td class="fname">Job</td><td><b>Double-click to rename</b>.</td></tr>
          <tr><td class="fname">Status / ETA</td><td>Live state, plus estimated time —
              remaining for the running job, or a prediction from past runs.</td></tr>
          <tr><td class="fname">Progress / Output</td><td>Per-row progress and the
              destination file.</td></tr>
        </table>
        <h3>Running &amp; managing</h3>
        <p class="muted"><b>Start</b> ticked jobs with <kbd>⌘R</kbd> (or all with
        <kbd>⌘⇧R</kbd>), <b>Stop</b> with <kbd>⌘.</kbd>. Duplicate with
        <kbd>⌘D</kbd>, delete with <kbd>⌫</kbd>, or right-click for <b>Set
        Priority</b>, <b>Requeue</b>, <b>Reveal</b>, <b>Open</b>, <b>Move</b>, and
        <b>Clear Queue</b>. A failed job <b>auto-retries once</b> after the others.
        Overall progress + ETA show below the queue, and every step is in the
        Live&nbsp;Logs.</p>
        """),
            ("Watch & Auto-render", """
        <h2>Watch &amp; Auto-render</h2>
        <p class="lead">Hands-off mode: point the app at a folder, mark the screens
        that matter, and every time fresh footage lands it imports, maps, and
        renders on its own — ideal for review loops and "drop a new cut, get a new
        preview" pipelines.</p>

        <h3>1 · The watch folder</h3>
        <p class="muted">Click the <b>clock</b> button under the Videos list (or set
        it in <a href="action:properties/Watch">Properties → Watch &amp; Auto-render</a>)
        and pick a folder. Anything you — or another app — drops there imports and
        <b>auto-maps</b> to a material by name. It's:</p>
        <table class="feat" width="100%">
          <tr><td class="fname">Version-aware</td><td>Drop <code>Screen_v2.mp4</code>
              next to <code>Screen_v1.mp4</code> and the newer version takes over the
              mapping — <b>latest wins</b>, no re-linking.</td></tr>
          <tr><td class="fname">Instant + reliable</td><td>Local drops are picked up
              the moment they land; folders on a <b>network share</b> or
              <b>Dropbox/cloud</b> are polled as a backstop, so nothing is missed.</td></tr>
          <tr><td class="fname">Copy-safe</td><td>A file is only imported once it has
              finished writing (its size holds steady) — never a half-copied clip.</td></tr>
          <tr><td class="fname">Cloud-aware</td><td>Dropbox/OneDrive <b>"online-only"</b>
              placeholders are skipped until their contents are actually downloaded.</td></tr>
        </table>

        <h3>2 · Mark render targets</h3>
        <p class="muted">Tell the app which screens an auto-render must cover:
        <b>right-click a material → Mark as Render Target</b>, or click the coloured
        <b>stripe</b> on the material's left edge. Targets are what the next step
        waits for.</p>

        <h3>3 · Auto-render</h3>
        <p class="muted">In <a href="action:properties/Watch">Properties → Watch &amp;
        Auto-render</a>, turn on <b>"Auto-render once every render-target screen has a
        clip"</b>. As soon as every target has footage, a single <b>multi-screen
        render</b> is created automatically (a burst of new versions is coalesced into
        one render, not one per file). Choose how it behaves:</p>
        <table class="feat" width="100%">
          <tr><td class="fname">Start automatically</td><td>On = it renders straight
              away. Off = the job is just <b>added to the Queue</b> for you to start.</td></tr>
          <tr><td class="fname">Output folder</td><td>Where renders go. Blank = a
              <code>PREVIZ</code> subfolder inside the watch folder (kept out of the
              scan, so previews never re-trigger themselves).</td></tr>
          <tr><td class="fname">Name</td><td>Filename pattern with tokens
              <code>{clip}</code> · <code>{scene}</code> · <code>{date}</code>.</td></tr>
        </table>

        <h3>4 · Delivery (optional)</h3>
        <p class="muted">Set a <b>Copy&nbsp;to</b> folder under <b>Delivery</b> and every
        finished render is also copied there — e.g. a synced review/hand-off folder —
        so collaborators get it without you lifting a finger. Blank = off.</p>

        <h3>Tuning</h3>
        <p class="muted">Two knobs in Properties: <b>poll interval</b> (how often a
        network/cloud folder is re-checked) and the <b>settle</b> window (how long a
        file's size must hold steady before import). Defaults suit most setups; raise
        the settle time for very large files copied slowly.</p>
        <p class="muted"><b>Tip:</b> watch + targets + auto-start + a delivery folder =
        a fully automatic "new footage → rendered preview in the review folder"
        pipeline.</p>

        <h3>Asset grouping → previz auto-export (advanced)</h3>
        <p class="muted">If your show uses a structured filename convention, the watch
        folder can export <b>one multi-screen previz render per asset</b> instead of
        mapping onto the current scene — drop 10 clips, get 5 assets. Turn it on in
        <a href="action:properties/Watch">Properties → Watch &amp; Auto-render → Asset
        grouping</a>.</p>
        <p class="muted">From a name like
        <code>PRJ001_D01_S01_A017_CENTER_ANIM_V003</code> it reads:</p>
        <table class="feat" width="100%">
          <tr><td class="fname">Setup (S##)</td><td>routes the render to that setup's
              <b>scene</b> (or the current scene if none is mapped).</td></tr>
          <tr><td class="fname">Asset (A###)</td><td>the <b>group key</b> — every screen
              of one asset assembles into a single render.</td></tr>
          <tr><td class="fname">Screen</td><td>maps to the <b>material</b> of the same name
              (or an override you set).</td></tr>
          <tr><td class="fname">Type (ANIM)</td><td>only this content type feeds a render;
              stills/maps are ignored.</td></tr>
          <tr><td class="fname">Version (V###)</td><td><b>newest wins</b>; a newer version
              updates that asset's job in place instead of piling up.</td></tr>
        </table>
        <p class="muted">Each asset exports as
        <code>{prj}_D{day}_S{setup}_A{asset}_PREVIZ_V{ver}</code> (the name template,
        parser regex, content type, screen→material map and per-setup scene are all
        editable). Queue-only by default, or auto-start with the toggle above.</p>
        """),
            ("Render Farm", """
        <h2>Render Farm (Deadline)</h2>
        <p class="lead">Submit Blender <i>and</i> Cinema&nbsp;4D jobs to a Thinkbox
        Deadline farm so frames spread across your nodes.</p>
        <h3>Connect</h3>
        <p class="muted">Enable the <b>Deadline</b> panel, set the <b>Repository</b>
        and (optionally) <b>deadlinecommand</b> paths — both auto-detected when
        possible — and hit <b>Test Connection</b>. All of this lives in
        <a href="action:properties/Deadline">Properties → Deadline</a>.</p>
        <h3>Submit</h3>
        <table class="feat" width="100%">
          <tr><td class="fname">Targeting</td><td>Pool, secondary pool, group,
              priority, department, machine limit and comment.</td></tr>
          <tr><td class="fname">Chunking</td><td><b>Manual</b> frames-per-task, or
              <b>Auto</b> (~5/10/20 min) sized from your render history.</td></tr>
          <tr><td class="fname">Engines</td><td>Blender jobs render via the worker on
              each node; <b>Cinema&nbsp;4D</b> jobs are baked into a self-contained
              scene and rendered with the licensed C4D command-line renderer, so node
              licensing just works.</td></tr>
          <tr><td class="fname">Nodes</td><td><i>Deadline → Farm Nodes…</i> lists the
              farm; right-click a queued job to <b>Set Priority</b> or <b>Requeue</b>.</td></tr>
        </table>
        <p class="muted"><b>One-time setup:</b> install the bundled
        <code>RenderMapperPro</code> Deadline plugin into your repository's
        <code>custom/plugins/</code>, and make sure each node is licensed for the
        renderer it runs.</p>
        """),
            ("Backends & Setup", """
        <h2>Backends &amp; Setup</h2>
        <p class="lead">Three render backends, chosen automatically by scene type.
        The app is self-contained — it fetches what it can.</p>
        <table class="feat" width="100%">
          <tr><td class="fname">Blender</td><td><code>.blend</code> / <code>.fbx</code>
              / <code>.usd</code> / <code>.obj</code> / <code>.abc</code>. If Blender
              isn't found the app offers to <b>download a managed copy</b> for you;
              or point it at your own in
              <a href="action:properties/Render Engines">Properties → Render Engines</a>.</td></tr>
          <tr><td class="fname">Cinema 4D + Redshift</td><td><code>.c4d</code>. Needs
              your own licensed Cinema&nbsp;4D + Redshift (detected via
              <code>c4dpy</code>) — it's commercial software the app can't install.</td></tr>
          <tr><td class="fname">Web / glTF</td><td><code>.glb</code> /
              <code>.gltf</code> via headless three.js — it fetches its own Chromium
              on first use. (You can also render a .glb through Blender.)</td></tr>
        </table>
        <p class="muted"><b>ffmpeg</b> is bundled (frame extraction + muxing).
        Updates are automatic — the app checks Releases on launch; see
        <a href="action:properties/Updates">Properties → Updates</a>.</p>
        """),
            ("Reports & Tips", """
        <h2>Reports, Logs &amp; Tips</h2>
        <p class="lead">Track cost and time, review outputs, get notified, and move
        fast.</p>
        <table class="feat" width="100%">
          <tr><td class="fname">Analytics</td><td>Every render records seconds/frame,
              total time and an estimated <b>cost</b> — set wattage + rate in
              <a href="action:power">Power &amp; Cost</a>, see runs in
              <a href="action:history">Render History</a>.</td></tr>
          <tr><td class="fname">Output review</td><td>Auto <b>contact sheets</b> per
              render, plus a shareable <b>HTML report</b> with timing, cost and
              thumbnails.</td></tr>
          <tr><td class="fname">Notifications</td><td>Ping on complete/fail via the
              system tray or a <b>Discord webhook</b> —
              <a href="action:notifications">set it up</a>.</td></tr>
          <tr><td class="fname">Live Logs</td><td>Filter by text or level; downloads
              and renders show a one-line <b>progress bar</b>; <b>Copy Diagnostics</b>
              grabs everything for a bug report.</td></tr>
        </table>
        <h3>Move faster</h3>
        <p class="muted">Press <kbd>⌘K</kbd> for the <b>command palette</b> to run
        anything by name, browse all <a href="action:shortcuts">keyboard
        shortcuts</a>, rearrange the workspace from <i>View → Layout</i>, and toggle
        light/dark in <i>View</i>.</p>
        """),
        ]

    def _show_quick_start(self) -> None:
        html = self._help_css() + """
        <h2>Quick Start</h2>
        <p class="lead">Map your videos onto a 3D scene's materials and render them —
        on your machine or a farm. Here's the whole flow in five steps.</p>

        <table class="steps" width="100%">
          <tr><td class="num" width="30">1</td>
              <td><b>Add your scene.</b> Drag a 3D file onto the Scene box (or
              <a href="action:properties">point the app at Blender</a> first, then
              <b>Browse</b>), and click <b>Scan Scene</b> to read its materials and cameras.
              <span class="muted">Blender (.blend, .fbx, .usd, .obj…), Cinema&nbsp;4D (.c4d)
              and glTF (.glb / .gltf) all work.</span></td></tr>
          <tr><td class="num">2</td>
              <td><b>Add your clips.</b> Drag videos into the Videos list (or click <b>Add</b>),
              then choose the <b>Camera</b> to render from.</td></tr>
          <tr><td class="num">3</td>
              <td><b>Link each clip to a material.</b> Easiest way: click <b>Auto-map</b> — it
              matches by name, so a <code>Screen</code> material grabs <code>Screen_v3.mp4</code>.
              Or select one of each and click the <b>link</b> button between the lists. A colored
              stripe marks every pair.</td></tr>
          <tr><td class="num">4</td>
              <td><b>Set the output.</b> It auto-fills next to your clip — adjust the file,
              resolution, frame range and format to taste.</td></tr>
          <tr><td class="num">5</td>
              <td><b>Queue &amp; render.</b> Click <b>Queue</b>, then <b>Start</b>
              (<kbd>⌘R</kbd>). Watch per-row progress and a live frame preview — or send the
              job to a farm.</td></tr>
        </table>

        <h3>Do more, automatically</h3>
        <table class="feat" width="100%">
          <tr><td class="fname">Auto-map</td>
              <td>Links clips to materials by name the moment you import them — only filling
              empty materials, never overwriting your manual links.</td></tr>
          <tr><td class="fname">Watch folder</td>
              <td>Point it at a folder and new clips import and map themselves; the newest
              version of a clip always wins. <a href="action:properties/Watch">Set it up&nbsp;→</a></td></tr>
          <tr><td class="fname">Auto-render</td>
              <td>Mark screens as targets; one multi-screen render queues automatically once
              every target has a clip. <a href="action:properties/Watch">Configure&nbsp;→</a></td></tr>
          <tr><td class="fname">Render farm</td>
              <td>Submit Blender &amp; Cinema&nbsp;4D jobs to a Thinkbox Deadline farm — frames
              spread across nodes. <a href="action:properties/Deadline">Connect&nbsp;→</a></td></tr>
          <tr><td class="fname">Cinema&nbsp;4D</td>
              <td>Open a .c4d and the renderer switches to Redshift, with Draft→Final speed
              presets in the Advanced panel.</td></tr>
          <tr><td class="fname">Cost &amp; reports</td>
              <td>Every render logs time, seconds/frame and energy cost, plus an HTML report and
              contact sheet. <a href="action:history">Open history&nbsp;→</a></td></tr>
          <tr><td class="fname">Notifications</td>
              <td>Get pinged in the system tray or a Discord webhook when a render finishes or
              fails. <a href="action:notifications">Set up&nbsp;→</a></td></tr>
          <tr><td class="fname">Audio</td>
              <td>Clips with sound show a speaker badge — click to mute one; the rest are mixed
              into the final video.</td></tr>
        </table>

        <h3>Good to know</h3>
        <p class="muted">Press <kbd>⌘K</kbd> for the command palette to run anything by name,
        browse all <a href="action:shortcuts">keyboard shortcuts</a>, or open
        <a href="action:properties">Properties</a> to set up Blender and Cinema&nbsp;4D.
        Drag a panel's tab to rearrange, tab or float it, rearrange the whole workspace from
        <i>View → Layout</i>, and switch light/dark in <i>View</i>.</p>
        """
        self._show_help_dialog("Quick Start", html)

    def _show_shortcuts_help(self) -> None:
        groups = [
            ("General", [
                ("⌘K", "Command palette — search &amp; run anything"),
                ("⌘,", "Properties &amp; Settings"),
                ("⌘Z", "Undo the last destructive action"),
            ]),
            ("Project", [
                ("⌘O", "Open a project"),
                ("⌘S", "Save the project"),
                ("⌘B", "Browse for a scene file"),
            ]),
            ("Render", [
                ("⌘R", "Start render (jobs ticked Run)"),
                ("⌘⇧R", "Start all queued jobs"),
                ("⌘.", "Stop the current render"),
                ("Space", "Play / pause the preview movie"),
            ]),
            ("Queue", [
                ("⌘D", "Duplicate selected job(s)"),
                ("⌫ / Delete", "Delete selected job(s) / clips"),
            ]),
        ]
        sections = ""
        for name, rows in groups:
            body = "".join(
                f"<tr><td width='110'><kbd>{k}</kbd></td><td>{d}</td></tr>" for k, d in rows)
            sections += f"<h3>{name}</h3><table width='100%'>{body}</table>"
        html = self._help_css() + f"""
        <h2>Keyboard Shortcuts</h2>
        <p class="lead">On Windows &amp; Linux, ⌘ is Ctrl. Queue shortcuts apply when the
        Queue panel has focus.</p>
        {sections}
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
            w.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lay.addWidget(w, 0, Qt.AlignmentFlag.AlignHCenter)
            return w

        lay.addWidget(_ImageView(_make_app_icon().pixmap(QSize(92, 92)), pal.window),
                      0, Qt.AlignmentFlag.AlignHCenter)

        name = centered(QLabel(APP_NAME))
        name.setStyleSheet(f"color:{pal.text}; font-size:19px; font-weight:700; margin-top:8px;")
        ver = centered(QLabel(f"Version {APP_VERSION}"))
        ver.setStyleSheet(f"color:{pal.text_muted}; font-size:12px;")
        desc = centered(QLabel("Automated video-texture mapping and headless\n"
                               "rendering — Blender, Cinema 4D and three.js."))
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
        self.scene_panel.watch_clips_ready.connect(self._on_watch_clips_ready)
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
        # Switching engine (e.g. three.js ↔ Blender for a .glb) re-adapts the panel.
        self.render_panel.engine_combo.currentIndexChanged.connect(lambda _i: self._adapt_renderer_panel())
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
        # three.js scene-lighting + burn-in change the rendered look → refresh preview.
        self.render_panel.web_light_preset_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.web_light_intensity_slider.valueChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.web_respect_lights_cb.stateChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.burn_in_cb.stateChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.ao_cb.stateChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.ao_distance_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.ao_factor_edit.textChanged.connect(lambda _v: self._on_settings_changed())

        self.deadline_panel.settings_changed.connect(lambda: self._on_settings_changed(preview=False))
        self.deadline_panel.test_connection_requested.connect(self._test_deadline_connection)
        # When the user ticks "Enable Deadline Submission" but it isn't configured,
        # jump them straight to the Deadline settings to fix it (clicked = user only).
        self.deadline_panel.use_dl_cb.clicked.connect(self._on_deadline_enabled_clicked)
        self.deadline_panel.export_requested.connect(self._export_deadline_files)

        self.render_panel.profile_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed(preview=False))
        self.scene_panel.scene_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.scene_panel.scene_edit.textChanged.connect(lambda _v: self._update_renderer_for_scene())
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
            # Big render-monitoring layout: Scene+Render left; on the right the
            # Queue and the live Preview are STACKED (both visible at once) above
            # the Logs — you watch the batch and the frame together.
            self.splitDockWidget(sc, q, Qt.Orientation.Horizontal)
            self.splitDockWidget(sc, rd, Qt.Orientation.Vertical)
            self.splitDockWidget(q, pv, Qt.Orientation.Vertical)
            self.splitDockWidget(pv, lg, Qt.Orientation.Vertical)
            self.tabifyDockWidget(rd, dl)
            self.tabifyDockWidget(rd, pr)
            rd.raise_()
            self.resizeDocks([sc, q], [430, 870], Qt.Orientation.Horizontal)
            self.resizeDocks([q, pv, lg], [520, 380, 160], Qt.Orientation.Vertical)
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
        else:  # "default" — Scene | Render | (Queue over Live Preview over Logs)
            self.splitDockWidget(sc, rd, Qt.Orientation.Horizontal)
            self.splitDockWidget(rd, q, Qt.Orientation.Horizontal)
            self.splitDockWidget(rd, pr, Qt.Orientation.Vertical)
            self.splitDockWidget(q, pv, Qt.Orientation.Vertical)   # queue on top, live frame below it
            self.splitDockWidget(pv, lg, Qt.Orientation.Vertical)  # logs tucked under the preview
            self.tabifyDockWidget(rd, dl)
            rd.raise_()
            q.raise_()
            self.resizeDocks([sc, rd, q], [380, 480, 620], Qt.Orientation.Horizontal)
            self.resizeDocks([rd, pr], [520, 240], Qt.Orientation.Vertical)
            self.resizeDocks([q, pv, lg], [340, 320, 150], Qt.Orientation.Vertical)

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
        self._custom_layout_state = bytes(
            self.saveState(LAYOUT_STATE_VERSION).toBase64().data()).decode("ascii")
        if hasattr(self, "restore_layout_action"):
            self.restore_layout_action.setEnabled(True)
        self._schedule_save()
        self._show_toast("Layout saved", "success")

    def _restore_custom_layout(self) -> None:
        if not self._custom_layout_state:
            return
        ok = False
        try:
            # restoreState returns False when the version tag doesn't match (e.g.
            # the layout was saved by a build with a different set of docks).
            ok = self.restoreState(
                QByteArray.fromBase64(self._custom_layout_state.encode("ascii")),
                LAYOUT_STATE_VERSION)
        except Exception:
            ok = False
        if ok:
            self._fit_to_screen(recenter=False)
            self._schedule_titlebar_sync()
        else:
            # Stale/incompatible layout → don't leave the window half-restored.
            self._apply_layout("default")
            self._show_toast("Saved layout was incompatible — reset to default", "warning")

    def event(self, e):  # type: ignore[override]
        if e.type() == QEvent.Type.LayoutRequest:
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
            _log.warning("failed to write to the on-disk log file", exc_info=True)

    def _init_blender(self) -> None:
        b = _find_blender(self._blender_path)
        if b:
            self._blender_path = b
            self._append_log(f"[app] Blender detected: {b}")
        else:
            self._append_log("[app] Blender not found — the app can download it for you.")
            # On a first launch the welcome dialog makes the offer (one clean
            # prompt); for a returning user with no Blender, ask here.
            if not getattr(self, "_is_first_run", False):
                self._prompt_install_runtime()

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

    def _show_properties_dialog(self, initial_tab: str | None = None) -> None:
        from dialogs import build_properties_dialog
        build_properties_dialog(self, initial_tab)

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
        if not scene:
            return
        if not file_exists(scene):
            self._show_toast(f"Scene file not found: {scene}", "warning")
            return
        is_c4d = is_c4d_scene(scene)
        is_web = is_web_scene(scene)
        # Cinema 4D scenes need c4dpy; web (.glb/.gltf) renders in-browser and
        # needs neither Blender nor c4dpy; everything else needs Blender.
        if is_web:
            blender = ""
        elif is_c4d:
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

    def _apply_accessible_names(self) -> None:
        """Give every icon-only button an accessible name so screen readers /
        the macOS Accessibility Inspector / UI automation announce it (a tooltip
        is NOT an accessible name in Qt). Reuses each button's existing tooltip."""
        from PySide6.QtWidgets import QPushButton
        for btn in self.findChildren(QPushButton):
            if not btn.text().strip() and btn.toolTip().strip() and not btn.accessibleName().strip():
                btn.setAccessibleName(btn.toolTip().strip())

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
        powered = QLabel(
            f'Powered by <span style="color:{self._palette.accent}; '
            f'font-weight:600;">Toy Robot Media</span>')
        powered.setStyleSheet(f"color:{self._palette.text_faint}; padding:0 10px;")
        sb.addPermanentWidget(powered)
        ver = QLabel(f"v{APP_VERSION}")
        ver.setStyleSheet(f"color:{self._palette.text_faint}; padding:0 12px;")
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
        if watching and self._asset_grouping.enabled:
            n = len(self._asset_group_jobs)
            done = sum(1 for jid, _v in self._asset_group_jobs.values()
                       if any(j.id == jid and j.status == "success" for j in self._jobs))
            self._sb_watch.setText(f"Watch ▸ {n} asset(s) · {done} done" if n else "Watch ▸ assembling")
        else:
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

    def _update_renderer_for_scene(self) -> None:
        """Reflect the scene type in the renderer dropdown the moment a scene is
        picked — Redshift for .c4d, three.js for .glb/.gltf, Blender otherwise —
        without waiting for a Scan."""
        s = self.scene_panel.scene_edit.text().strip().lower()
        if not s:
            return
        self._set_renderer_options(is_c4d_scene(s), is_web_scene(s))

    def _set_renderer_options(self, is_c4d: bool, is_web: bool = False, detected: str = "") -> None:
        """Populate the renderer dropdown with the engines that apply to the
        loaded scene: Redshift for .c4d, three.js for .glb/.gltf, Blender else."""
        combo = self.render_panel.engine_combo
        # C4D → Redshift only. .glb/.gltf → three.js OR Blender (Blender imports
        # the glTF), three.js as the default. .blend/other → Blender engines.
        if is_web:
            items = ["WEB_THREEJS", "CYCLES", "BLENDER_EEVEE"]
        elif is_c4d:
            items = ["Redshift"]
        else:
            items = ["CYCLES", "BLENDER_EEVEE"]
        cur = self.render_panel.engine_value()
        if detected in items:
            target = detected                 # restore the scene's saved renderer
        elif is_web:
            target = "WEB_THREEJS"            # .glb defaults to three.js (switchable to Blender)
        elif cur in items:
            target = cur                      # keep the user's engine across same-type scenes
        else:
            target = items[0]
        # Adapt the settings to the TARGET engine, not the extension — so picking
        # Blender for a .glb swaps the three.js controls for Blender's.
        self.render_panel.set_renderer(target == "Redshift", target == "WEB_THREEJS")
        if self.render_panel.engine_values() != items:
            combo.blockSignals(True)
            self.render_panel.populate_engines(items)
            self.render_panel.set_engine_value(target)
            combo.blockSignals(False)
        elif self.render_panel.engine_value() != target:
            combo.blockSignals(True)
            self.render_panel.set_engine_value(target)
            combo.blockSignals(False)

    def _adapt_renderer_panel(self) -> None:
        """Re-adapt the settings panel when the user switches engine in the
        dropdown — e.g. choosing Blender for a .glb swaps the three.js scene-
        lighting/outputs for Blender's device + colour-management controls."""
        eng = self.render_panel.engine_value()
        self.render_panel.set_renderer(eng == "Redshift", eng == "WEB_THREEJS")

    def _on_discovery(self, materials: list, cameras: list, settings: dict) -> None:
        self._material_aspects = dict(settings.get("material_aspects") or {})
        # Render-preflight inputs (None = unknown / not reported for this backend).
        self._scene_has_lighting = settings.get("has_lighting")
        rs = settings.get("redshift_materials")
        self._redshift_materials = set(rs) if rs is not None else None
        self._scanned_scene = self.scene_panel.scene_edit.text().strip()
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

        # The renderer dropdown reflects the scene type: Redshift for .c4d,
        # three.js for .glb/.gltf, Blender engines otherwise.
        scene_l = self.scene_panel.scene_edit.text().strip().lower()
        is_c4d = is_c4d_scene(scene_l)
        is_web = is_web_scene(scene_l)
        self._set_renderer_options(is_c4d, is_web, settings.get("renderer", "") if settings else "")

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

    def _sync_grouping_mode(self) -> None:
        """Switch the watch folder between auto-map (normal) and asset-grouping."""
        if hasattr(self, "scene_panel"):
            self.scene_panel.set_grouping_mode(self._asset_grouping.enabled)

    def _on_watch_clips_ready(self, paths: list) -> None:
        """Asset-grouping watch path: parse the ready clips by the naming
        convention and build one previz render job per (setup, asset). The newest
        version of each screen wins; an existing job for that asset is updated
        in place when a newer version lands, so re-renders don't pile up."""
        if not self._asset_grouping.enabled or not paths:
            return
        try:
            groups = group_clips(list(paths), self._asset_grouping)
        except Exception as exc:
            self._append_log(f"[app] Asset grouping failed: {exc}")
            return
        if not groups:
            return
        cur_scene = self.scene_panel.scene_edit.text().strip()
        watch_folder, _en = self.scene_panel.get_watch_folder()
        touched: set[int] = set()
        created = updated = 0
        for g in groups:
            scene = (self._asset_grouping.setup_to_scene.get(g.setup) or cur_scene).strip()
            if not scene:
                self._append_log(
                    f"[app] {g.prj} S{g.setup:02d} A{g.asset:03d}: no scene mapped for this "
                    f"setup — set one in Properties → Watch & Auto-render. Skipped.")
                continue
            key = (g.setup, g.asset)
            prev = self._asset_group_jobs.get(key)
            existing = None
            if prev is not None:
                prev_id, prev_ver = prev
                existing = next((j for j in self._jobs if j.id == prev_id), None)
                if existing is not None and prev_ver >= g.version:
                    continue   # already queued at this version or newer
            asn = [MaterialVideoAssignment(mat, clip, VIDEO_MAPPING_MODE_EMISSION)
                   for mat, clip in g.material_assignments(self._asset_grouping.screen_to_material)]
            if not asn:
                continue
            if existing is None:
                job = RenderJob(id=self._next_job_id)
                self._next_job_id += 1
                self._jobs.insert(0, job)
                created += 1
            else:
                job = existing
                updated += 1
            job.video_path = asn[0].video_path
            self._make_job_snapshot(job, asn)
            job.scene_path = scene   # override: this setup's scene, not the loaded one
            base = g.output_name(self._asset_grouping.output_template)
            out_fmt, _c = OUTPUT_PROFILES.get(job.output_profile or "H264 MP4", ("MPEG4", "H264"))
            ext = ext_for_format(out_fmt) or ".mp4"
            out_dir = self._autorender_output or (
                os.path.join(watch_folder, "PREVIZ") if watch_folder
                else str(Path(scene).parent / "PREVIZ"))
            self.scene_panel.set_watch_ignore_dir(out_dir)   # never re-ingest our own renders
            job.output_path = str(Path(out_dir) / f"{base}{ext}")
            job.output_input = job.output_path
            job.custom_label = True
            job.label = base
            job.status, job.progress, job.error, job.selected = "idle", 0.0, "", True
            self._asset_group_jobs[key] = (job.id, g.version)
            touched.add(job.id)
        if not touched:
            return
        self._refresh_queue_view()
        self._schedule_save()
        self._append_log(
            f"[app] Asset grouping: {created} new + {updated} updated previz job(s) "
            f"from {len(groups)} asset group(s).")
        if self._autorender_start and not self._is_rendering:
            self._start_render(only_job_ids=touched)

    def _preview_assembly(self, cfg: AssetGroupingConfig | None = None) -> None:
        """Dry-run the asset-grouping on the watch folder's current clips and show
        exactly what WOULD be assembled — per asset: screen → material → clip →
        version — plus any skipped clips and why. Makes the auto-assembly trustable
        before it ever fires a render. ``cfg`` lets the Properties dialog preview its
        in-progress edits; defaults to the saved grouping config."""
        cfg = cfg or self._asset_grouping
        folder, _en = self.scene_panel.get_watch_folder()
        if not folder or not os.path.isdir(folder):
            QMessageBox.information(self, "Preview Assembly",
                "Choose a watch folder first (the watch button in the Scene panel).")
            return
        exts = VIDEO_EXTENSIONS | IMAGE_MEDIA_EXTENSIONS
        try:
            clips = sorted(str(p) for p in Path(folder).iterdir()
                           if p.is_file() and p.suffix.lower() in exts)
        except OSError:
            clips = []
        groups = group_clips(clips, cfg)
        known_mats = set(self._discovered_materials)
        cur_scene = self.scene_panel.scene_edit.text().strip()

        pal = self._palette
        rows = []
        used: set[str] = set()
        for g in groups:
            scene = (cfg.setup_to_scene.get(g.setup) or cur_scene).strip()
            scene_name = Path(scene).name if scene else "⚠ no scene mapped"
            out = g.output_name(cfg.output_template)
            screen_rows = []
            for material, clip in g.material_assignments(cfg.screen_to_material):
                used.add(clip)
                pc = parse_clip(clip, cfg.pattern)
                ver = f"V{pc.version:03d}" if pc else "?"
                screen = pc.screen if pc else "?"
                warn = "" if (not known_mats or material in known_mats) else \
                    f" <span style='color:{pal.danger}'>(not in scene)</span>"
                screen_rows.append(
                    f"<tr><td style='color:{pal.text_muted}'>{screen}</td>"
                    f"<td>→ <b>{material}</b>{warn}</td>"
                    f"<td style='color:{pal.text_muted}'>← {Path(clip).name} · {ver}</td></tr>")
            rows.append(
                f"<p style='margin:14px 0 2px'><b>Asset A{g.asset:03d}</b> "
                f"<span style='color:{pal.text_muted}'>· setup {g.setup} · scene {scene_name}</span><br>"
                f"<span style='color:{pal.accent}'>{out}</span></p>"
                f"<table cellpadding='2'>{''.join(screen_rows)}</table>")

        skipped = []
        for c in clips:
            if c in used:
                continue
            pc = parse_clip(c, cfg.pattern)
            if pc is None:
                why = "doesn't match the filename pattern"
            elif (pc.type or "").upper() != (cfg.content_type or "ANIM").upper():
                why = f"type={pc.type} (only {cfg.content_type} is grouped)"
            else:
                why = "superseded by a newer version"
            skipped.append(f"<li>{Path(c).name} <span style='color:{pal.text_muted}'>— {why}</span></li>")

        summary = (f"<b>{len(groups)} asset(s)</b> from {len(clips)} clip(s) in "
                   f"<span style='color:{pal.text_muted}'>{folder}</span>")
        body = "".join(rows) if rows else f"<p style='color:{pal.text_muted}'>No assets matched the pattern.</p>"
        skip_html = (f"<p style='margin-top:16px'><b>Skipped ({len(skipped)})</b></p>"
                     f"<ul style='margin:2px 0'>{''.join(skipped)}</ul>") if skipped else ""
        html = (f"<div style='font-size:13px'><p>{summary}</p>{body}{skip_html}</div>")

        dlg = QDialog(self)
        dlg.setWindowTitle("Preview Assembly — dry run")
        dlg.setMinimumSize(560, 480)
        lay = QVBoxLayout(dlg)
        view = QTextBrowser()
        view.setHtml(html)
        view.setStyleSheet(f"QTextBrowser{{border:1px solid {pal.border}; border-radius:8px; "
                           f"background:{pal.surface}; padding:10px;}}")
        lay.addWidget(view)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        lay.addLayout(row)
        dlg.exec()

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

    def _preview_signature(self) -> tuple:
        """A hashable fingerprint of everything that changes the *rendered preview
        frame*. Output-only knobs (codec, video quality, fps, frame step, render
        device, render-scale %) are deliberately excluded — they never alter the
        single frame shown, so changing them must not trigger a re-render. Frame
        start/end ARE included: they set where the clip samples at the previewed
        frame."""
        sp, rp = self.scene_panel, self.render_panel
        o = rp.render_options()
        asn = tuple((a.material_name, a.video_path, a.mapping_mode)
                    for a in sp.get_assignments())
        return (
            sp.scene_edit.text().strip(),
            asn,
            sp.camera_combo.currentText(),
            o.engine, o.width, o.height,
            o.samples, o.use_denoise, o.film_transparent, o.burn_in,
            o.ao_enabled, o.ao_distance, o.ao_factor,
            o.color_view_transform, o.color_exposure, o.color_gamma,
            o.frame_start, o.frame_end,
            o.rs_min_samples, o.rs_threshold, o.rs_gi_enabled,
            o.rs_gi_bounces, o.rs_ray_depth,
            o.web_lighting_preset, o.web_lighting_intensity, o.web_respect_scene_lights,
            self.preview_panel.current_frame(),
            round(self.preview_panel.preview_scale(), 4),
        )

    def _fire_auto_preview(self) -> None:
        if not self.preview_panel.auto_btn.isChecked() or self._is_rendering:
            return
        if not self._preview_ready():
            return
        # Only re-render when something that affects the rendered frame changed —
        # tweaking output codec/fps/device/etc. leaves the frame identical.
        sig = self._preview_signature()
        if sig == self._last_preview_sig:
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


    def _unsaved_floating_changes(self) -> bool:
        """True when the UI holds a mapped setup that isn't backed by any queued
        job (no active job) — loading another job would silently lose it."""
        return (
            self._active_job_id is None
            and not self._loading_job_into_ui
            and bool(self.scene_panel.scene_edit.text().strip())
            and bool(self.scene_panel.get_assignments())
        )


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


    @staticmethod
    def _friendly_error_hint(text: str) -> str:
        return friendly_error_hint(text)   # logic in core.reporting (UI-free, tested)


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
        return disk_space_warnings(pending)   # logic in core.jobs (UI-free, tested)

    def _render_quality_warnings(self, pending: list[RenderJob]) -> list[str]:
        """Non-blocking heads-up that a render will likely look wrong, from the last
        scan: a Blender scene with no lighting renders non-emissive geometry black,
        and a C4D clip only shows on a Redshift node material. Only the currently-
        scanned scene is checked — that's the one we have discovery info for."""
        warns: list[str] = []
        scanned = (self._scanned_scene or "").strip()
        using = [j for j in pending if scanned and (j.scene_path or "").strip() == scanned]
        if not using:
            return warns
        if is_c4d_scene(scanned):
            if self._redshift_materials is not None:
                for j in using:
                    bad = sorted({a.material_name for a in j.material_assignments
                                  if a.material_name and a.video_path
                                  and a.material_name not in self._redshift_materials})
                    if bad:
                        warns.append(
                            f"“{j.label or f'Job {j.id}'}”: {', '.join(bad)} isn't a Redshift "
                            "material — its clip won't appear. Convert it to a Redshift node "
                            "material in Cinema 4D.")
        elif self._scene_has_lighting is False and any(
                not uses_web_backend(scanned, j.render_options.engine if j.render_options else "")
                for j in using):
            warns.append(
                f"“{Path(scanned).name}” has no lights or world lighting — non-emissive geometry "
                "will render black. Add a light (or world lighting) in the scene.")
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

        # Which jobs are we actually rendering? Each carries its OWN scene, so the
        # render backends are resolved from THESE jobs — a queue can mix .blend /
        # .glb / .c4d, and each needs its own executable. (Picking the backend from
        # just the active scene left other-backend jobs with an empty/wrong one,
        # so a queued .c4d would wrongly fall through to Blender, etc.)
        if only_job_ids is not None:
            selected_ids = set(only_job_ids)
        else:
            selected_ids = set(self.queue_panel.selected_job_ids()) if not render_all else set(j.id for j in self._jobs)
        pending = [j for j in self._jobs if j.id in selected_ids and j.status != "success"]
        if not pending:
            QMessageBox.information(self, "Nothing To Do", "No queued jobs selected (or all selected jobs already successful).")
            return

        # Cinema 4D renders via c4dpy/Redshift; web (.glb/.gltf) in a headless
        # browser (no Blender/c4dpy); everything else via Blender.
        def _job_engine(j: RenderJob) -> str:
            return j.render_options.engine if j.render_options else ""
        _needs_c4d = any(is_c4d_scene(j.scene_path) for j in pending)
        _needs_blender = any(
            not is_c4d_scene(j.scene_path) and not uses_web_backend(j.scene_path, _job_engine(j))
            for j in pending)
        if _needs_c4d:
            c4dpy = self._ensure_c4dpy(interactive=True)
            if not c4dpy:
                return
        else:
            c4dpy = ""
        if _needs_blender:
            blender = self._ensure_blender(interactive=True) or ""
            if not blender:
                return
        else:
            blender = ""
        _is_c4d = _needs_c4d   # the c4d worker script is resolved from this below

        errs = self._preflight()
        if errs:
            QMessageBox.critical(self, "Preflight Failed", "\n".join(errs))
            return

        # Claim the rendering state NOW — the modal dialogs below spin the Qt event
        # loop, so without this an auto-render signal could re-enter _start_render
        # and double-start (orphaning a RenderThread). Reset on every early return.
        self._is_rendering = True

        # Non-blocking warning if the frame range overshoots the source video,
        # the output disk looks too small, or a Deadline pool/group is unknown.
        warnings = (self._frame_range_warnings(pending)
                    + self._disk_space_warnings(pending)
                    + self._deadline_warnings(pending)
                    + self._render_quality_warnings(pending))
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
        # Bundled ffmpeg path for the workers: C4D always muxes its movie with it,
        # and the Blender worker needs it for its PNG-sequence movie fallback when
        # Blender's own FFMPEG writer is unavailable (version-dependent).
        _ffmpeg = find_ffmpeg_tool("ffmpeg") or ""

        # Live preview: one temp JPEG the worker rewrites each frame.
        self._preview_path = ""
        if getattr(self, "_preview_enabled", True):
            self._preview_path = str(Path(tempfile.gettempdir()) / "rmp_live_preview.jpg")
            try:
                Path(self._preview_path).unlink(missing_ok=True)
            except Exception:
                _log.debug("could not remove stale live-preview temp file", exc_info=True)

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
                _log.warning("could not create the render output directory", exc_info=True)

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
                ffmpeg_path=_ffmpeg,
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

        # Surface the QUEUE so you watch the batch burn down (statuses/ETA/progress)
        # rather than yanking focus to the single-frame preview. The live frame sits
        # alongside the queue (split layouts) or one tab away (compact ones).
        self.queue_dock.show()
        self.queue_dock.raise_()
        if self._preview_path:
            self.preview_panel.clear_preview()
            self.preview_dock.show()
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
            _log.debug("could not reveal exported .blend in file manager", exc_info=True)

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
        is_c4d = is_c4d_scene(scene)
        # Preview a .glb via Blender unless three.js is the chosen engine.
        is_web = uses_web_backend(scene, self.render_panel.engine_value())
        if is_web:
            c4dpy = ""
            blender = ""
        elif is_c4d:
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
        # Remember exactly what we're rendering so the auto-preview can skip a
        # re-render when nothing frame-relevant changes afterwards.
        self._last_preview_sig = self._preview_signature()
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
        _eng = "three.js" if is_web else ("C4D/Redshift" if is_c4d else "Blender")
        self._append_log(f"[app] Preview: frame={fs} camera={camera!r} scale={pct}% engine={_eng}")
        self._preview_thread = PreviewFrameThread(blender, worker, cfg, out_dir,
                                                  c4dpy=c4dpy, c4d_worker=c4d_worker)
        self._preview_thread.log.connect(self._append_log)
        self._preview_thread.log.connect(self._on_preview_progress)
        self._preview_thread.done.connect(self._on_preview_frame_done)
        self.preview_panel.start_render_progress()   # thin bar under the preview
        self._preview_thread.start()

    def _on_preview_progress(self, line: str) -> None:
        """Drive the thin bar under the preview from the worker's log: determinate
        when the engine reports a % / fraction, otherwise it stays indeterminate."""
        prog = LogsPanel._parse_progress(line)
        if prog is not None:
            self.preview_panel.set_render_progress(prog[0])

    def _on_preview_frame_done(self, path: str, error: str) -> None:
        self.preview_panel.end_render_progress()
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

    def _load_history(self) -> list[dict]:
        """Render history, cached in memory — _job_etas reads this on every (per-
        frame) queue refresh, so re-parsing the JSON from disk each time put real
        I/O on the UI thread. The cache is invalidated whenever history is written."""
        cache = getattr(self, "_history_cache", None)
        if cache is not None:
            return cache
        data: list[dict] = []
        try:
            if HISTORY_PATH.exists():
                parsed = json.loads(HISTORY_PATH.read_text())
                if isinstance(parsed, list):
                    data = parsed
        except Exception:
            _log.debug("could not read render history file", exc_info=True)
        self._history_cache = data
        return data

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
                    self._transcode_extra_outputs(j)   # master → proxy deliverables
                if status in {"success", "failed", "cancelled"}:
                    j.attempts += 1
                    start = (started or {}).get(job_id)
                    dur = (time.monotonic() - start) if start else 0.0
                    self._job_durations[job_id] = dur
                    self._record_history(j, status, dur)
                break
        self._refresh_queue_view()
        self._update_progress_caption()

    def _transcode_extra_outputs(self, job: RenderJob) -> None:
        """After a job's primary render succeeds, produce its extra deliverables by
        transcoding the finished movie with the bundled ffmpeg (off-thread). One
        render → many formats (e.g. a ProRes master plus an H.264 review proxy),
        without re-rendering the 3D scene."""
        extras = list(getattr(job, "extra_output_profiles", None) or [])
        src = (job.output_path or "").strip()
        if not extras or job.use_deadline or not src or not Path(src).exists():
            return
        ff = find_ffmpeg_tool("ffmpeg")
        if not ff:
            return
        src_path = Path(src)
        targets = []
        for prof in extras:
            spec = OUTPUT_PROFILES.get(prof)
            if not spec:
                continue
            fmt, codec = spec
            if fmt not in ("MPEG4", "QUICKTIME"):
                continue   # only movie deliverables are transcoded from a movie
            dst = src_path.with_name(f"{src_path.stem}_{prof.split()[0].lower()}{ext_for_format(fmt) or '.mp4'}")
            if dst != src_path:
                targets.append((dst, codec))
        if not targets:
            return
        label = job.label or f"Job {job.id}"

        def work() -> None:
            for dst, codec in targets:
                try:
                    vcodec = "prores_ks" if codec == "PRORES" else (
                        "libx265" if codec == "H265" else "libx264")
                    cmd = [ff, "-y", "-loglevel", "error", "-i", str(src_path), "-c:v", vcodec]
                    if codec == "PRORES":
                        cmd += ["-profile:v", "3", "-pix_fmt", "yuv422p10le"]
                    else:
                        cmd += ["-crf", "20", "-pix_fmt", "yuv420p"]
                    cmd += ["-c:a", "aac", "-movflags", "+faststart", str(dst)]
                    subprocess.run(cmd, capture_output=True, timeout=1800,
                                   creationflags=subprocess_creation_flags())
                    ok = dst.exists() and dst.stat().st_size > 0
                    self._delivery_log.emit(
                        f"[output] {label}: {'wrote' if ok else 'failed'} {dst.name}",
                        "success" if ok else "warning")
                except Exception as exc:
                    self._delivery_log.emit(f"[output] {label}: {dst.name} failed — {exc}", "warning")
        threading.Thread(target=work, daemon=True).start()

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
            hist = list(self._load_history())   # cached + isinstance-guarded
            hist.insert(0, entry)
            hist = hist[:200]
            atomic_write_text(HISTORY_PATH, json.dumps(hist, indent=2))
            self._history_cache = hist           # keep the cache in step with disk
        except Exception:
            _log.warning("failed to save render history", exc_info=True)

    @staticmethod
    def _fmt_dur(seconds: float) -> str:
        return format_duration(seconds)   # logic in core.reporting (UI-free, tested)

    def _job_etas(self) -> dict[int, str]:
        """Per-job ETA strings for the queue's ETA column: remaining time for the
        running job (from elapsed + progress), blank for finished ones (their real
        time is in Render History), and a from-history prediction for the rest."""
        need_pred = any(
            j.status not in ("running", "success", "failed", "cancelled") for j in self._jobs)
        history = self._load_history() if need_pred else []
        return {j.id: self._job_eta_text(j, history) for j in self._jobs}

    def _job_eta_text(self, job: RenderJob, history: list[dict]) -> str:
        if job.status == "running":
            started = getattr(self, "_job_started", {}).get(job.id)
            if started and job.progress > 1:
                elapsed = time.monotonic() - started
                return f"~{self._fmt_dur(elapsed * (100.0 - job.progress) / job.progress)}"
            return "…"
        if job.status in ("success", "failed", "cancelled"):
            return ""
        ro = job.render_options
        if ro is None:
            return "—"
        scene = Path(job.scene_path).name if job.scene_path else ""
        fc = max(1, ro.frame_end - ro.frame_start + 1)
        pred = predict_total_seconds(history, scene, fc)
        return f"~{self._fmt_dur(pred)}" if pred else "—"

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
                urllib.request.urlopen(req, timeout=10, context=_ssl_context()).close()
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
                _log.debug("could not schedule sleep-on-finish action", exc_info=True)

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
                    _log.debug("contact-sheet generation failed for an output", exc_info=True)
            self._sheets_built.emit(built)

        self._sheet_thread = FuncThread(lambda: work(targets))
        self._sheet_thread.start()

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
            _log.debug("could not open path in the default application", exc_info=True)

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
            # A project file is meant to be shared — strip machine-local secrets
            # (the Discord webhook) so they never travel with it.
            data = self._profile_dict()
            data.pop("discord_webhook", None)
            atomic_write_text(p, json.dumps(data, indent=2))
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
            ("Toggle Light/Dark Theme", lambda: self._toggle_theme(self._effective_theme_mode() != "light")),
            ("Follow System Appearance", lambda: self._follow_system_theme(True)),
            ("User Guide", self._show_user_guide),
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
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up) and lst.count():
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
                atomic_write_text(HISTORY_PATH, "[]")
                self._history_cache = []
            except Exception:
                _log.warning("failed to clear render history", exc_info=True)
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


    def _delete_preset_entry(self, entry: object) -> None:
        if not isinstance(entry, dict):
            return
        p = str(entry.get("path", "")).strip()
        if p:
            self._delete_preset_path(p)


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
            _log.debug("could not open the presets folder in file manager", exc_info=True)

    def _profile_dict(self) -> dict:
        videos = self.scene_panel.get_videos()
        layout_state = bytes(self.saveState(LAYOUT_STATE_VERSION).toBase64().data()).decode("ascii")
        layout_geometry = bytes(self.saveGeometry().toBase64().data()).decode("ascii")

        # RenderJob is a dataclass — asdict() serializes every field (incl. nested
        # render_options + material_assignments) and auto-tracks new fields, so this
        # never drifts from the model. The loader reads fields defensively (jd.get).
        jobs_data = [dataclasses.asdict(j) for j in self._jobs]

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
            "check_updates_on_launch": self._check_updates_on_launch,
            "skipped_update": self._skipped_update,
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
            "asset_grouping": self._asset_grouping.to_dict(),
            "restore_session_on_launch": self._restore_session_on_launch,
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
        return migrate_profile(d, PROFILE_VERSION, self._append_log)   # logic in core.jobs

    def _apply_profile_data(self, d: dict) -> None:
        d = self._migrate_profile(d)
        # Accent stays the brand orange; honor the saved theme choice.
        tm = str(d.get("theme_mode", self._theme_mode))
        if tm in ("dark", "light", "system"):
            self._theme_mode = tm
        try:
            self._power_watts = float(d.get("power_watts", self._power_watts))
            self._power_rate = float(d.get("power_rate", self._power_rate))
        except (TypeError, ValueError):
            _log.debug("invalid power/cost values in profile; using defaults", exc_info=True)
        self._notify_desktop = bool(d.get("notify_desktop", self._notify_desktop))
        self._discord_webhook = str(d.get("discord_webhook", self._discord_webhook) or "")
        self._check_updates_on_launch = bool(d.get("check_updates_on_launch", self._check_updates_on_launch))
        self._skipped_update = str(d.get("skipped_update", self._skipped_update) or "")
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
            self.deadline_panel.dl_sec_pool_combo.addItems(["", *pools])

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
                    _log.debug("scene-folder search for a moved clip failed", exc_info=True)
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
            _log.debug("invalid watch-folder values in profile; using defaults", exc_info=True)
        self._autorender_enabled = bool(d.get("autorender_enabled", False))
        self._autorender_start = bool(d.get("autorender_start", False))
        self._autorender_output = str(d.get("autorender_output", "") or "")
        self._autorender_pattern = str(d.get("autorender_pattern", "") or "{clip}_PREVIZ")
        self._deliver_dir = str(d.get("deliver_dir", "") or "")
        self._asset_grouping = AssetGroupingConfig.from_dict(d.get("asset_grouping"))
        self._sync_grouping_mode()
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
                _log.debug("could not restore saved window geometry", exc_info=True)
        if state_b64:
            ok = False
            try:
                # Version-tagged: a layout saved by a build with a different dock
                # set returns False rather than restoring into a broken state.
                ok = self.restoreState(
                    QByteArray.fromBase64(state_b64.encode("ascii")), LAYOUT_STATE_VERSION)
            except Exception:
                _log.debug("could not restore saved dock layout", exc_info=True)
            if ok:
                self._schedule_titlebar_sync()
            else:
                # Incompatible/old layout — keep the default preset already applied
                # at startup instead of a half-restored window.
                self._apply_layout("default")

        # Invariant: a non-empty queue always has exactly one active job.
        self._ensure_active_selection()

        # Clean-launch (default): the profile is fully restored above so nothing is
        # lost, then we stash that session and open an empty workspace — like New
        # in a document app. "Reopen Last Session" brings it back. Restore-on-launch
        # users keep the old behaviour.
        self._restore_session_on_launch = bool(d.get("restore_session_on_launch", False))
        if not self._restore_session_on_launch:
            snap = self._capture_workspace()
            if snap.get("scene") or snap.get("jobs"):
                self._last_session = snap
            self._new_session(confirm=False, announce=False)

    def _capture_workspace(self) -> dict:
        """Snapshot the editable session — scene, clips, mappings, targets, camera
        and the queue — so it can be cleared and later reopened intact."""
        import copy
        sp = self.scene_panel
        return {
            "scene": sp.scene_edit.text(),
            "videos": list(sp.get_videos()),
            "assignments": [(a.material_name, a.video_path, a.mapping_mode)
                            for a in sp.get_assignments()],
            "muted": list(sp.get_muted_videos()),
            "targets": list(sp.get_targets()),
            "camera": sp.camera_combo.currentText(),
            "jobs": copy.deepcopy(self._jobs),
            "active": self._active_job_id,
        }

    def _apply_workspace(self, snap: dict) -> None:
        """Restore a workspace snapshot from _capture_workspace (paths already
        resolved, so no re-scan needed)."""
        sp = self.scene_panel
        self._loading_job_into_ui = True
        try:
            sp.scene_edit.setText(snap.get("scene", ""))
            sp.set_videos(list(snap.get("videos", [])))
            sp.set_assignments([MaterialVideoAssignment(*t) for t in snap.get("assignments", [])])
            sp.set_muted_videos(list(snap.get("muted", [])))
            sp.set_targets(list(snap.get("targets", [])))
            cam = snap.get("camera", "")
            if cam:
                idx = sp.camera_combo.findText(cam)
                if idx >= 0:
                    sp.camera_combo.setCurrentIndex(idx)
            self._jobs = snap.get("jobs", []) or []
            self._active_job_id = snap.get("active")
        finally:
            self._loading_job_into_ui = False
        self._refresh_job_outputs()
        self._refresh_queue_view()
        self._ensure_active_selection()
        self._schedule_save()

    def _new_session(self, confirm: bool = True, announce: bool = True) -> None:
        """Start a fresh, empty workspace — clear the scene, clips, mappings,
        targets and the queue (like File → New). Guarded when there's work."""
        if self._is_rendering:
            QMessageBox.information(self, "Render In Progress",
                                    "Stop rendering before starting a new session.")
            return
        has_work = bool(self.scene_panel.scene_edit.text().strip()
                        or self.scene_panel.get_videos() or self._jobs)
        if confirm and has_work:
            resp = QMessageBox.question(
                self, "New",
                "Start a new session? The current scene, clips and queue will be "
                "cleared (your saved projects and recents are kept).",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if resp != QMessageBox.StandardButton.Yes:
                return
        if confirm and has_work:
            self._last_session = self._capture_workspace()   # let Reopen undo an explicit New
        sp = self.scene_panel
        self._loading_job_into_ui = True
        try:
            sp.set_assignments([])
            sp.set_targets([])
            sp.set_muted_videos([])
            sp.set_videos([])
            sp.scene_edit.setText("")
        finally:
            self._loading_job_into_ui = False
        self._jobs = []
        self._active_job_id = None
        self._asset_group_jobs = {}
        self._refresh_job_outputs()
        self._refresh_queue_view()
        self._schedule_save()
        if announce:
            self._show_toast("New session", "info")

    def _reopen_last_session(self) -> None:
        """Bring back the session that was open before a clean launch or New."""
        if not self._last_session:
            self._show_toast("No previous session to reopen", "info")
            return
        snap = self._last_session
        self._apply_workspace(snap)
        self._show_toast("Reopened last session", "success")

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
            _log.warning("failed to load saved profile; using defaults", exc_info=True)
        # Widgets were built under the construction-time theme; re-apply the saved
        # choice (system/light/dark may differ from what was painted) and sync the
        # View-menu actions.
        self._restyle_all()
        self._sync_theme_menu()

    def _maybe_first_run(self) -> None:
        """A one-time welcome on the very first launch: show what's detected and
        point new users at setup + Quick Start."""
        if not getattr(self, "_is_first_run", False):
            return
        blender = _find_blender(self._blender_path)
        ffmpeg = find_ffmpeg_tool("ffmpeg")
        can_fetch = _runtime_download_spec() is not None
        blender_line = (
            f"✓ {Path(blender).name}" if blender
            else ("— the app will download it for you" if can_fetch
                  else "✗ not found — install Blender and set it in Properties"))
        lines = [
            f"Blender   {blender_line}",
            f"Cinema 4D   {'✓ detected' if self._c4dpy_path else '— not detected (optional)'}",
            f"ffmpeg   {'✓ bundled' if ffmpeg else '✗ not found — frame extraction will be limited'}",
        ]
        tail = ("\n\nNew here? Open the Quick Start for a quick tour of the whole flow."
                if blender else
                "\n\nNo Blender yet? Click “Download Blender” and the app fetches a "
                "managed copy — nothing else to install.")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle(f"Welcome to {APP_NAME}")
        box.setText(f"Welcome to {APP_NAME}!")
        box.setInformativeText(
            "Map your videos onto a 3D scene's materials and render them — on your "
            "machine or a farm.\n\nWhat's ready on this computer:\n\n"
            + "\n".join(lines) + tail)
        qs = box.addButton("Open Quick Start", QMessageBox.ButtonRole.ActionRole)
        # When Blender is missing, lead with the one-click auto-download.
        dl = box.addButton("Download Blender", QMessageBox.ButtonRole.ActionRole) \
            if (not blender and can_fetch) else None
        loc = box.addButton("Locate Blender…", QMessageBox.ButtonRole.ActionRole) \
            if not blender else None
        go = box.addButton("Get Started", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(dl or go)
        box.exec()
        clicked = box.clickedButton()
        if clicked is qs:
            self._show_quick_start()
        elif dl is not None and clicked is dl:
            self._install_managed_runtime()
        elif loc is not None and clicked is loc:
            self._show_properties_dialog()
        self._save_profile()   # ensure a profile exists so this won't show again

    def _save_profile(self) -> None:
        try:
            # Atomic + owner-only (the profile may hold a webhook secret).
            atomic_write_text(PROFILE_PATH, json.dumps(self._profile_dict(), indent=2), mode=0o600)
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
        _log.debug("could not set the macOS application name", exc_info=True)


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
            _log.warning("failed to write crash traceback to the log file", exc_info=True)
        if showing["active"]:
            return
        showing["active"] = True
        try:
            box = QMessageBox(window)
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle(f"{APP_NAME} — Unexpected Error")
            box.setText("Something went wrong. The app will keep running, but the last "
                        "action may not have completed.")
            box.setInformativeText(f"{exc_type.__name__}: {exc}")
            box.setDetailedText(text)
            copy_btn = box.addButton("Copy Details", QMessageBox.ButtonRole.ActionRole)
            log_btn = box.addButton("Open Log", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Close)
            box.exec()
            clicked = box.clickedButton()
            if clicked is copy_btn:
                QApplication.clipboard().setText(text)
            elif clicked is log_btn:
                try:
                    reveal_in_file_manager(LOG_PATH)
                except Exception:
                    _log.debug("could not reveal the log file in file manager", exc_info=True)
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
    # Route stdlib logging (used by core/ modules) into the Live Logs + existing
    # file log, and stamp a per-launch banner so sessions are easy to find. No
    # file handler here — _append_log is the sole writer of LOG_PATH.
    from core.logging_setup import add_callback_handler, get_logger, setup_logging
    setup_logging(version=APP_VERSION)
    add_callback_handler(lambda _level, msg: win._append_log(msg))
    get_logger().info("%s %s ready on %s", APP_NAME, APP_VERSION, sys.platform)
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


def _web_selftest(argv: list[str]) -> int:
    """Headless self-test of the web render backend (used to validate the frozen
    bundle): --web-selftest <scene.glb> <clip> <out.mp4>. No GUI."""
    from core.models import JobConfig, MaterialVideoAssignment, RenderOptions
    from core.web_render import run_web_job
    scene, clip, out = argv[0], argv[1], argv[2]
    job = JobConfig(
        scene_path=scene, video_path=clip, target_material="Screen", target_camera="",
        output_path=out,
        render=RenderOptions(width=480, height=270, fps=24, frame_start=1, frame_end=12),
        material_assignments=[MaterialVideoAssignment("Screen", clip)])
    rc = run_web_job(job, on_log=print)
    print(f"[selftest] rc={rc} out_exists={Path(out).exists()}")
    return rc


if __name__ == "__main__":
    if "--web-selftest" in sys.argv:
        i = sys.argv.index("--web-selftest")
        raise SystemExit(_web_selftest(sys.argv[i + 1:i + 4]))
    run_qt_app()
