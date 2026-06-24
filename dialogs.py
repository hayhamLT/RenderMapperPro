"""Dialogs extracted from the app_qt god-object.

build_properties_dialog() is the Properties & Settings dialog, moved here verbatim
(window references via the `win` parameter) so the main window shrinks while
behavior stays identical. app_qt helpers are imported locally to avoid a cycle.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from core.utils import find_deadlinecommand, subprocess_creation_flags
from media import _find_ffprobe, find_ffmpeg_tool
from ui_dialogs import error, inform, warn
from workers import DeadlineQueryThread


def build_properties_dialog(win, initial_tab: str | None = None) -> None:
    """The Properties & Settings dialog. Operates on the main window ``win``."""
    from app_qt import (
        APP_VERSION,
        LOG_PATH,
        PROFILE_PATH,
        _blender_version_status,
        _find_blender,
        _find_c4dpy,
    )
    from core.runtime import BLENDER_RUNTIME_VERSION, _norm_blender

    dlg = QDialog(win)
    dlg.setWindowTitle("Properties & Settings")
    dlg.setMinimumWidth(720)
    dlg.setMinimumHeight(460)
    # Open tall enough that the common tabs fit without scrolling; tabs that
    # overflow (Watch & Auto-render) now scroll rather than squeeze.
    dlg.resize(820, 760)
    root = QVBoxLayout(dlg)
    tabs = QTabWidget()
    root.addWidget(tabs)

    def group(title: str) -> tuple[QGroupBox, QVBoxLayout]:
        """A titled, framed section — the modern replacement for a bare all-caps
        header. Returns the box (add it to the tab) and its content layout."""
        box = QGroupBox(title)
        gl = QVBoxLayout(box)
        gl.setSpacing(8)
        return box, gl

    def disclose(checkbox: QCheckBox) -> tuple[QWidget, QVBoxLayout]:
        """A body whose visibility follows a master checkbox (progressive
        disclosure) — advanced options stay hidden until the feature is on."""
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(18, 0, 0, 0)
        bl.setSpacing(8)
        body.setVisible(checkbox.isChecked())
        checkbox.toggled.connect(body.setVisible)
        return body, bl

    def _tab(title: str) -> QVBoxLayout:
        # Each tab scrolls instead of squeezing. Without this, a short window
        # compresses the page and word-wrapped hints get clipped/overlapped
        # (the tall Watch & Auto-render tab was unreadable). The page keeps its
        # natural height and a scrollbar appears only when it doesn't fit.
        content = QWidget()
        v = QVBoxLayout(content)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(10)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        tabs.addTab(scroll, title)
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
        lbl.setStyleSheet(f"color:{win._palette.text_muted}; font-size:11px;")
        return lbl

    # ── General ──────────────────────────────────────────────────────
    lay = _tab("General")
    g, gl = group("When a render finishes")
    behave_row = QHBoxLayout()
    behave_row.addWidget(QLabel("Then:"))
    when_combo = QComboBox()
    _when_opts = [("Do nothing", "nothing"), ("Quit the app", "quit"), ("Sleep the computer", "sleep")]
    when_combo.addItems([lbl for lbl, _ in _when_opts])
    _vals = [v for _, v in _when_opts]
    when_combo.setCurrentIndex(_vals.index(win._when_done) if win._when_done in _vals else 0)
    behave_row.addWidget(when_combo)
    behave_row.addStretch()
    gl.addLayout(behave_row)
    lay.addWidget(g)

    g, gl = group("Preview")
    preview_cb = QCheckBox("Show a live frame preview while rendering")
    preview_cb.setChecked(win._preview_enabled)
    gl.addWidget(preview_cb)
    gl.addWidget(hint("Renders the current frame as it goes so you can watch progress. "
                      "Turn off for a small speed-up on heavy scenes."))
    lay.addWidget(g)

    g, gl = group("Startup")
    restore_cb = QCheckBox("Reopen the last session on launch")
    restore_cb.setChecked(getattr(win, "_restore_session_on_launch", False))
    gl.addWidget(restore_cb)
    gl.addWidget(hint("Off (default): the app opens to a clean, empty workspace — use "
                      "Profile → New (⌘N) to start fresh anytime, the Scene picker's "
                      "recents to reopen a scene, or Reopen Last Session to bring back "
                      "your last scene + queue. On: it restores your last scene, mappings "
                      "and queue automatically."))
    lay.addWidget(g)
    lay.addStretch()

    # ── Render Engines ───────────────────────────────────────────────
    lay = _tab("Render Engines")
    lay.addWidget(hint("Scenes route to a renderer automatically by type — Blender for "
                       ".blend / .fbx / .usd / .obj…, and Cinema 4D + Redshift for .c4d."))

    g, gl = group("Blender")
    blender_row = QHBoxLayout()
    blender_edit = QLineEdit(win._blender_path)
    blender_edit.setPlaceholderText("Path to the Blender executable")
    blender_locate = QPushButton("Locate")
    blender_row.addWidget(QLabel("Executable:"))
    blender_row.addWidget(blender_edit, 1)
    blender_row.addWidget(blender_locate)
    gl.addLayout(blender_row)

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
                warn(
                    dlg, "Not a Blender App",
                    "That location doesn't contain Blender. Pick the Blender app itself "
                    "(e.g. /Applications/Blender.app) or its executable.")
    blender_locate.clicked.connect(do_locate_blender)

    detect_row = QHBoxLayout()
    detect_btn = QPushButton("Auto-detect")
    detect_btn.setToolTip("Search the usual install locations for Blender")
    install_btn = QPushButton("Install Managed Blender…")
    install_btn.setToolTip(f"Download a win-contained Blender {BLENDER_RUNTIME_VERSION} runtime")
    detect_row.addWidget(detect_btn)
    detect_row.addWidget(install_btn)
    detect_row.addStretch()
    gl.addLayout(detect_row)
    blender_ver_lbl = hint("")
    gl.addWidget(blender_ver_lbl)

    def do_check_version() -> None:
        exe = blender_edit.text().strip()
        if not exe or not Path(exe).exists():
            blender_ver_lbl.setText("No valid Blender path set.")
            return
        try:
            out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15,
                                 creationflags=subprocess_creation_flags())
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
    install_btn.clicked.connect(win._install_managed_runtime)
    blender_edit.editingFinished.connect(do_check_version)
    do_check_version()
    lay.addWidget(g)

    g, gl = group("Cinema 4D + Redshift")
    c4d_row = QHBoxLayout()
    c4dpy_edit = QLineEdit(win._c4dpy_path)
    c4dpy_edit.setPlaceholderText("Path to c4dpy (Cinema 4D's headless Python)")
    c4d_locate = QPushButton("Locate")
    c4d_row.addWidget(QLabel("c4dpy:"))
    c4d_row.addWidget(c4dpy_edit, 1)
    c4d_row.addWidget(c4d_locate)
    gl.addLayout(c4d_row)
    c4d_detect_row = QHBoxLayout()
    c4d_detect_btn = QPushButton("Auto-detect")
    c4d_detect_btn.setToolTip("Search the usual install locations for Cinema 4D's c4dpy")
    c4d_detect_row.addWidget(c4d_detect_btn)
    c4d_detect_row.addStretch()
    gl.addLayout(c4d_detect_row)
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
    gl.addWidget(c4d_status_lbl)
    _c4d_refresh()
    lay.addWidget(g)
    lay.addStretch()

    # ── Watch & Auto-render ──────────────────────────────────────────
    lay = _tab("Watch && Auto-render")

    g, gl = group("Watch folder")
    gl.addWidget(hint("Drop clips into a watch folder and they import + map by name "
                      "automatically (latest version wins)."))
    _wi, _ws = win.scene_panel.get_watch_options()
    watch_form = QFormLayout()
    watch_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
    watch_interval_edit = QLineEdit(f"{_wi / 1000:g}")
    watch_interval_edit.setFixedWidth(80)
    watch_interval_edit.setToolTip("How often the watch folder is polled, in seconds.")
    watch_settle_edit = QLineEdit(f"{_ws:g}")
    watch_settle_edit.setFixedWidth(80)
    watch_settle_edit.setToolTip("How long a file's size must stay steady before it's imported "
                                 "(guards against half-copied files).")
    watch_form.addRow("Poll interval (s):", watch_interval_edit)
    watch_form.addRow("Stability window (s):", watch_settle_edit)
    gl.addLayout(watch_form)
    lay.addWidget(g)

    g, gl = group("Auto-render")
    ar_enable_cb = QCheckBox("Auto-render once every render-target screen has a clip")
    ar_enable_cb.setChecked(win._autorender_enabled)
    ar_enable_cb.setToolTip("Mark screens as render targets (right-click a material, or click its "
                            "left stripe). When every target has a clip — or newer versions arrive — "
                            "a single render covering all targets is queued (debounced).")
    gl.addWidget(ar_enable_cb)
    gl.addWidget(hint("Mark targets by right-clicking a material → Set as Render Target, or click the "
                      "stripe on its left. Linking a clip also targets it. The render waits until "
                      "every target has a clip."))
    ar_body, ar_body_l = disclose(ar_enable_cb)   # options appear once auto-render is on
    ar_start_cb = QCheckBox("Start it automatically (otherwise just add it to the queue)")
    ar_start_cb.setChecked(win._autorender_start)
    ar_body_l.addWidget(ar_start_cb)
    ar_out_row = QHBoxLayout()
    ar_out_edit = QLineEdit(win._autorender_output)
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
    ar_body_l.addLayout(ar_out_row)
    ar_pat_row = QHBoxLayout()
    ar_pat_edit = QLineEdit(win._autorender_pattern)
    ar_pat_edit.setToolTip("Output filename. Tokens: {clip} (first mapped clip), {scene}, {date}.")
    ar_pat_row.addWidget(QLabel("Name:"))
    ar_pat_row.addWidget(ar_pat_edit, 1)
    ar_pat_hint = QLabel("tokens: {clip} {scene} {date}")
    ar_pat_hint.setStyleSheet(f"color:{win._palette.text_muted}; font-size:11px;")
    ar_pat_row.addWidget(ar_pat_hint)
    ar_body_l.addLayout(ar_pat_row)
    gl.addWidget(ar_body)
    lay.addWidget(g)

    g, gl = group("Delivery")
    gl.addWidget(hint("After a render finishes, copy the output(s) into this folder "
                      "automatically — e.g. a synced delivery/review folder. Blank = off."))
    dlv_row = QHBoxLayout()
    dlv_edit = QLineEdit(win._deliver_dir)
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
    gl.addLayout(dlv_row)
    lay.addWidget(g)
    lay.addStretch()

    # ── Previz Assembly ──────────────────────────────────────────────
    # Split off its own tab: it's a distinct, advanced watch-folder workflow
    # and was the bulk of what made the Watch tab overwhelming.
    lay = _tab("Previz Assembly")
    _ag = win._asset_grouping
    g, gl = group("Group watch-folder clips into previz renders")
    gl.addWidget(hint("Build one previz render per asset from a filename convention like "
                      "PRJ001_D01_S01_A017_CENTER_ANIM_V003 — dropped clips are grouped by "
                      "setup + asset, each screen maps to its material, and the newest "
                      "version of each screen wins. Replaces auto-map while it's on."))
    ag_enable_cb = QCheckBox("Group watch-folder clips into previz renders by filename")
    ag_enable_cb.setChecked(_ag.enabled)
    gl.addWidget(ag_enable_cb)
    ag_body, ag_body_l = disclose(ag_enable_cb)   # the whole config hides until it's on

    ag_body_l.addWidget(QLabel("Filename pattern:"))
    ag_pat_edit = QLineEdit(_ag.pattern)
    ag_pat_edit.setPlaceholderText("{Project}_D{Day#}_S{Setup#}_A{Asset#}_{Screen}_{Type}_V{Version#}")
    from ui_widgets import FilenamePatternBuilder
    ag_body_l.addWidget(FilenamePatternBuilder(ag_pat_edit))   # visual chip editor (writes the field below)
    ag_body_l.addWidget(ag_pat_edit)                           # canonical text form — editable directly
    ag_body_l.addWidget(hint("Build it with the chips above (click a chip to rename, set Text or Number, "
                             "mark optional, reorder or delete), or just type it: {Field} = text, "
                             "{Field#} = number, {Field#?} = optional. Recognised fields: Project, Day, "
                             "Setup, Asset, Screen, Type, Version. (A raw regex still works too.)"))

    # Live preview: type a sample filename and see exactly what each field captures —
    # or where the pattern stops matching — so it's tunable without knowing regex.
    ag_sample_edit = QLineEdit()
    ag_sample_edit.setPlaceholderText("Try a sample filename, e.g. PRJ001_D01_S01_A017_CENTER_ANIM_V003")
    ag_body_l.addWidget(ag_sample_edit)
    ag_preview_lbl = QLabel()
    ag_preview_lbl.setWordWrap(True)
    ag_body_l.addWidget(ag_preview_lbl)

    def _update_pattern_preview(*_a) -> None:
        from core.naming import preview as _preview_pattern
        res = _preview_pattern(ag_pat_edit.text().strip(), ag_sample_edit.text().strip())
        if res.ok:
            shown = "    ".join(f"{k} = {v}" for k, v in res.fields.items())
            ag_preview_lbl.setText("✓  " + shown)
            ag_preview_lbl.setStyleSheet(f"color:{win._palette.success}; font-size:11px;")
        else:
            ag_preview_lbl.setText("•  " + res.error)
            ag_preview_lbl.setStyleSheet(f"color:{win._palette.warning}; font-size:11px;")
    ag_pat_edit.textChanged.connect(_update_pattern_preview)
    ag_sample_edit.textChanged.connect(_update_pattern_preview)
    _update_pattern_preview()

    ag_form = QFormLayout()
    ag_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
    ag_type_edit = QLineEdit(_ag.content_type)
    ag_type_edit.setFixedWidth(90)
    ag_type_edit.setPlaceholderText("ANIM")
    ag_tmpl_edit = QLineEdit(_ag.output_template)
    ag_tmpl_edit.setPlaceholderText("{prj}_D{day}_S{setup}_A{asset}_PREVIZ_V{ver}")
    ag_screen_edit = QLineEdit(", ".join(f"{k}={v}" for k, v in _ag.screen_to_material.items()))
    ag_screen_edit.setPlaceholderText("CENTER=Center_Screen, LEFT=Left_Screen   (blank = code is the material name)")
    ag_setup_edit = QLineEdit(", ".join(f"{k}={v}" for k, v in sorted(_ag.setup_to_scene.items())))
    ag_setup_edit.setPlaceholderText("1=/scenes/StageA.blend, 2=/scenes/StageB.blend   (blank = current scene)")
    ag_form.addRow("Content type:", ag_type_edit)
    ag_form.addRow("Output name:", ag_tmpl_edit)
    ag_form.addRow("Screen → material:", ag_screen_edit)
    ag_form.addRow("Setup → scene:", ag_setup_edit)
    ag_body_l.addLayout(ag_form)

    def _preview_assembly_dry_run() -> None:
        from core.asset_grouping import GroupingConfig

        def _pairs(text: str) -> dict:
            out: dict[str, str] = {}
            for part in text.split(","):
                if "=" in part and part.split("=", 1)[0].strip():
                    k, v = part.split("=", 1)
                    out[k.strip()] = v.strip()
            return out
        tmp = GroupingConfig(
            enabled=True,
            pattern=ag_pat_edit.text().strip() or _ag.pattern,
            content_type=ag_type_edit.text().strip() or "ANIM",
            output_template=ag_tmpl_edit.text().strip() or _ag.output_template,
            screen_to_material=_pairs(ag_screen_edit.text()),
            setup_to_scene={int(k): v for k, v in _pairs(ag_setup_edit.text()).items() if k.isdigit()},
        )
        win._preview_assembly(tmp)
    ag_preview_btn = QPushButton("Preview assembly (dry run)…")
    ag_preview_btn.setToolTip("Show exactly what would be built from the watch folder's current clips")
    ag_preview_btn.clicked.connect(_preview_assembly_dry_run)
    ag_body_l.addWidget(ag_preview_btn)
    gl.addWidget(ag_body)
    lay.addWidget(g)
    lay.addStretch()

    # ── Updates ──────────────────────────────────────────────────────
    lay = _tab("Updates")
    g, gl = group("Software updates")
    gl.addWidget(hint("When a newer version is released, the app shows a popup with the "
                      "release notes so you can download it — nothing installs without "
                      "your say-so."))
    upd_status = QLabel(f"This build is v{APP_VERSION}.")
    upd_status.setStyleSheet(f"color:{win._palette.text_muted}; font-size:12px;")
    gl.addWidget(upd_status)

    launch_check_cb = QCheckBox("Check for updates on launch")
    launch_check_cb.setChecked(getattr(win, "_check_updates_on_launch", True))
    gl.addWidget(launch_check_cb)
    gl.addWidget(hint("Looks for a newer release a few seconds after the app starts and "
                      "pops the update notice if one is found. Turn off to only check "
                      "manually with the button below."))

    upd_check_btn = QPushButton("Check for Updates Now")
    upd_check_btn.clicked.connect(lambda: win._check_for_updates(manual=True))
    upd_row = QHBoxLayout()
    upd_row.addWidget(upd_check_btn)
    upd_row.addStretch()
    gl.addLayout(upd_row)
    lay.addWidget(g)
    lay.addStretch()

    # ── Deadline ─────────────────────────────────────────────────────
    lay = _tab("Deadline")
    lay.addWidget(hint("Submit Blender and Cinema 4D jobs to a Thinkbox Deadline farm. "
                       "Leave blank to render locally."))
    g, gl = group("Configuration")

    # Repo Path
    repo_row = QHBoxLayout()
    repo_edit = QLineEdit(win._deadline_repo_path)
    repo_edit.setPlaceholderText("Default repository (or browse path)")
    repo_locate = QPushButton("Locate")
    repo_row.addWidget(QLabel("Repository Path:"))
    repo_row.addWidget(repo_edit, 1)
    repo_row.addWidget(repo_locate)
    gl.addLayout(repo_row)

    def do_locate_repo() -> None:
        chosen = QFileDialog.getExistingDirectory(dlg, "Select Deadline Repository Path", repo_edit.text() or "")
        if chosen:
            repo_edit.setText(chosen)
    repo_locate.clicked.connect(do_locate_repo)

    # Command Path
    cmd_row = QHBoxLayout()
    cmd_edit = QLineEdit(win._deadline_command_path)
    cmd_edit.setPlaceholderText("Path to deadlinecommand executable (optional)")
    cmd_locate = QPushButton("Locate")
    cmd_row.addWidget(QLabel("Command Path:   "))
    cmd_row.addWidget(cmd_edit, 1)
    cmd_row.addWidget(cmd_locate)
    gl.addLayout(cmd_row)

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
    repo_help.setStyleSheet(f"color:{win._palette.text_muted}; font-size:11px;")
    gl.addWidget(repo_help)

    # Name Template
    template_row = QHBoxLayout()
    template_edit = QLineEdit(win._deadline_job_name_template)
    template_edit.setPlaceholderText("e.g. Render Mapper Pro Job - {scene_name}")
    template_row.addWidget(QLabel("Name Template:  "))
    template_row.addWidget(template_edit, 1)
    gl.addLayout(template_row)

    # Comment
    comment_row = QHBoxLayout()
    comment_edit = QLineEdit(win._deadline_comment)
    comment_edit.setPlaceholderText("Optional job comment")
    comment_row.addWidget(QLabel("Job Comment:    "))
    comment_row.addWidget(comment_edit, 1)
    gl.addLayout(comment_row)
    lay.addWidget(g)

    g, gl = group("Connection")
    status_lbl = QLabel("Connection status: Not tested")
    status_lbl.setStyleSheet(f"color: {win._palette.text_faint}; font-size: 11px; font-weight: bold;")
    gl.addWidget(status_lbl)

    # Buttons Row
    diag_btn_layout = QHBoxLayout()
    test_conn_btn = QPushButton("Test Connection")
    export_files_btn = QPushButton("Export Job Files")
    diag_btn_layout.addWidget(test_conn_btn)
    diag_btn_layout.addWidget(export_files_btn)
    diag_btn_layout.addStretch()
    gl.addLayout(diag_btn_layout)
    lay.addWidget(g)
    lay.addStretch()

    # ── Diagnostics ──────────────────────────────────────────────────
    lay = _tab("Diagnostics")
    g, gl = group("Bundled tools")
    ff_lbl = QLabel(f"ffmpeg:   {find_ffmpeg_tool('ffmpeg') or 'not found'}\n"
                    f"ffprobe:  {_find_ffprobe() or 'not found'}")
    ff_lbl.setStyleSheet(f"color:{win._palette.text_muted}; font-size:11px;")
    ff_lbl.setWordWrap(True)
    gl.addWidget(ff_lbl)
    lay.addWidget(g)

    g, gl = group("Files & logs")
    data_dir = PROFILE_PATH.parent
    data_row = QHBoxLayout()
    data_row.addWidget(QLabel("App data folder:"))
    data_path = QLineEdit(str(data_dir))
    data_path.setReadOnly(True)
    data_row.addWidget(data_path, 1)
    open_data_btn = QPushButton("Open")
    open_data_btn.clicked.connect(lambda: _open_path(data_dir))
    data_row.addWidget(open_data_btn)
    gl.addLayout(data_row)
    diag_tools = QHBoxLayout()
    open_log_btn = QPushButton("Open Logs Folder")
    open_log_btn.clicked.connect(lambda: _open_path(LOG_PATH.parent))
    copy_diag_btn = QPushButton("Copy Diagnostics")
    copy_diag_btn.clicked.connect(win._copy_diagnostics)
    diag_tools.addWidget(open_log_btn)
    diag_tools.addWidget(copy_diag_btn)
    diag_tools.addStretch()
    gl.addLayout(diag_tools)
    lay.addWidget(g)
    lay.addStretch()

    # Dialog buttons live below the tabs.
    btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    root.addWidget(btns)

    # Handle Ok/Cancel
    def on_accept() -> None:
        win._blender_path = blender_edit.text().strip()
        win._c4dpy_path = c4dpy_edit.text().strip()
        win._deadline_repo_path = repo_edit.text().strip()
        win._deadline_command_path = cmd_edit.text().strip()
        win._deadline_job_name_template = template_edit.text().strip()
        win._deadline_comment = comment_edit.text().strip()

        # Sync hidden widgets in panel if needed
        win.deadline_panel.dl_cmd_edit.setText(win._deadline_command_path)
        win.deadline_panel.dl_repo_edit.setText(win._deadline_repo_path)
        win.deadline_panel.dl_name_template_edit.setText(win._deadline_job_name_template)
        win.deadline_panel.dl_comment_edit.setText(win._deadline_comment)

        # Behaviour
        win._restore_session_on_launch = restore_cb.isChecked()
        win._when_done = _vals[when_combo.currentIndex()]
        _menu_label = {"nothing": "Do Nothing", "quit": "Quit App", "sleep": "Sleep Computer"}.get(win._when_done)
        if _menu_label and hasattr(win, "_when_actions") and _menu_label in win._when_actions:
            win._when_actions[_menu_label].setChecked(True)
        if hasattr(win, "preview_action"):
            win.preview_action.setChecked(preview_cb.isChecked())
        else:
            win._preview_enabled = preview_cb.isChecked()

        # Watch / ingest options
        def _to_float(s, d):
            try:
                return float(s)
            except ValueError:
                return d
        interval_ms = int(max(1.0, _to_float(watch_interval_edit.text().strip(), 3.0)) * 1000)
        settle_s = max(0.0, _to_float(watch_settle_edit.text().strip(), 2.0))
        win.scene_panel.set_watch_options(interval_ms, settle_s)

        # Auto-render (targets)
        win._autorender_enabled = ar_enable_cb.isChecked()
        win._autorender_start = ar_start_cb.isChecked()
        win._autorender_output = ar_out_edit.text().strip()
        win._autorender_pattern = ar_pat_edit.text().strip() or "{clip}_PREVIZ"
        win._deliver_dir = dlv_edit.text().strip()

        # Asset grouping
        def _parse_pairs(text: str) -> dict:
            out: dict[str, str] = {}
            for part in text.split(","):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    if k.strip():
                        out[k.strip()] = v.strip()
            return out
        _ag = win._asset_grouping
        _ag.enabled = ag_enable_cb.isChecked()
        _ag.pattern = ag_pat_edit.text().strip() or _ag.pattern
        _ag.content_type = ag_type_edit.text().strip()
        _ag.output_template = ag_tmpl_edit.text().strip() or _ag.output_template
        _ag.screen_to_material = _parse_pairs(ag_screen_edit.text())
        _ag.setup_to_scene = {int(k): v for k, v in _parse_pairs(ag_setup_edit.text()).items()
                              if k.isdigit()}
        win._sync_grouping_mode()

        # Updates
        win._check_updates_on_launch = launch_check_cb.isChecked()

        win._save_profile()    # persist immediately so settings survive a quick quit
        dlg.accept()

    btns.accepted.connect(on_accept)
    btns.rejected.connect(dlg.reject)

    # Handle testing connection and exporting files inside dialog. The query
    # runs off-thread (DeadlineQueryThread) so the dialog never freezes — the
    # modal event loop still delivers the result signal while it's open.
    def _apply_props_test_result(res: dict) -> None:
        ok = bool(res.get("ok"))
        dp = win.deadline_panel
        if ok:
            pools = res.get("pools", [])
            current_pool = dp.dl_pool_combo.currentText()
            current_sec_pool = dp.dl_sec_pool_combo.currentText()
            dp.dl_pool_combo.clear()
            dp.dl_sec_pool_combo.clear()
            dp.dl_pool_combo.addItems(pools)
            dp.dl_sec_pool_combo.addItems(["", *pools])
            if current_pool:
                dp.dl_pool_combo.setCurrentText(current_pool)
            if current_sec_pool:
                dp.dl_sec_pool_combo.setCurrentText(current_sec_pool)

            groups = res.get("groups", [])
            current_group = dp.dl_group_combo.currentText()
            dp.dl_group_combo.clear()
            dp.dl_group_combo.addItems(["", *groups])
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
                status_lbl.setStyleSheet(f"color: {win._palette.success}; font-size: 11px; font-weight: bold;")
                inform(dlg, "Deadline Connection", "Successfully connected to Deadline repository and updated pools, groups, and machine list!")
            else:
                status_lbl.setText("Connection status: Connection failed")
                status_lbl.setStyleSheet(f"color: {win._palette.danger}; font-size: 11px; font-weight: bold;")
                warn(dlg, "Deadline Warning",
                     res.get("error", "") or "deadlinecommand failed.")
        except RuntimeError:
            pass   # dialog already closed

    def run_test_connection() -> None:
        cmd = cmd_edit.text().strip() or find_deadlinecommand() or "deadlinecommand"
        status_lbl.setText("Connection status: Testing...")
        status_lbl.setStyleSheet(f"color: {win._palette.warning}; font-size: 11px; font-weight: bold;")
        if not Path(cmd).exists() and not shutil.which(cmd):
            status_lbl.setText("Connection status: deadlinecommand not found")
            status_lbl.setStyleSheet(f"color: {win._palette.danger}; font-size: 11px; font-weight: bold;")
            error(dlg, "Deadline Connection Error", f"deadlinecommand not found at {cmd}.\nPlease check your Thinkbox Deadline installation.")
            return
        if win._props_deadline_thread is not None and win._props_deadline_thread.isRunning():
            return
        test_conn_btn.setEnabled(False)
        win._props_deadline_thread = DeadlineQueryThread(cmd, repo_edit.text().strip())
        win._props_deadline_thread.result.connect(_apply_props_test_result)
        win._props_deadline_thread.start()

    test_conn_btn.clicked.connect(run_test_connection)
    export_files_btn.clicked.connect(win._export_deadline_files)

    if initial_tab:
        # Tolerant match so callers (incl. in-app help links) can pass a clean
        # name: ignore the '&' mnemonic markers, case, and allow a prefix match.
        def _norm(s: str) -> str:
            return s.replace("&", "").strip().lower()
        want = _norm(initial_tab)
        for i in range(tabs.count()):
            label = _norm(tabs.tabText(i))
            if label == want or label.startswith(want):
                tabs.setCurrentIndex(i)
                break

    dlg.exec()
