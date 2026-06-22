"""The app's dock panels, extracted from app_qt.py.

ScenePanel (scene + clips + mapping + watch folder), RenderPanel (renderer-aware
settings), DeadlinePanel (farm submission), QueuePanel (job list),
PresetBrowserPanel, LogsPanel, and PreviewPanel (live frame preview + player).
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import (
    QEvent,
    QFileSystemWatcher,
    QObject,
    QSize,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget
    from PySide6.QtWidgets import QStackedWidget
    _HAS_MULTIMEDIA = True
except Exception:
    _HAS_MULTIMEDIA = False

import icons
import theme as T
from core.logging_setup import get_logger
from core.models import (
    VIDEO_MAPPING_MODE_BASE_COLOR,
    VIDEO_MAPPING_MODE_EMISSION,
    MaterialVideoAssignment,
    RenderJob,
    RenderOptions,
    is_c4d_scene,
    uses_web_backend,
)
from core.utils import (
    IMAGE_MEDIA_EXTENSIONS,
    OUTPUT_PROFILES,
    OUTPUT_TOKENS,
    VIDEO_EXTENSIONS,
    auto_match_media_to_materials,
    is_cloud_placeholder,
    reconcile_versions,
    subprocess_creation_flags,
)
from media import (
    MOD_LABEL,
    _audio_probe_cache,
    file_manager_name,
    find_ffmpeg_tool,
    video_has_audio,
)
from theme import LINK_COLORS, active_palette
from ui_widgets import (
    ROLE_HAS_AUDIO,
    ROLE_MAP_COLOR,
    ROLE_MUTED,
    ROLE_TARGET,
    ROLE_VIDEO_PATH,
    AudioBadgeDelegate,
    HintListWidget,
    HintTableWidget,
    MaterialListWidget,
    ScenePathLineEdit,
    TargetStripeDelegate,
    VideoListWidget,
)

_log = get_logger(__name__)


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
    target_set_ready = Signal(list)  # all targets have clips + set changed → auto-render
    watch_clips_ready = Signal(list)  # grouping mode: ready clip paths → app builds previz jobs
    targets_changed = Signal(list)   # render-target materials changed (persist)
    assignments_cleared = Signal(list)  # mappings about to be cleared (for undo)
    videos_removed = Signal(int, dict)  # (count, pre-removal snapshot) for undo

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._materials: list[str] = []
        self._videos: list[str] = []
        self._assignments: list[MaterialVideoAssignment] = []
        self._recent_scenes: list[str] = []
        self._muted_videos: set[str] = set()
        # Cross-highlight: partner rows lit up for the hovered/selected row.
        self._hl_materials: set[str] = set()
        self._hl_videos: set[str] = set()
        self._hover_material: str | None = None
        self._hover_video: str | None = None
        self._watch_folder: str = ""
        self._watch_seen: object = {}   # last poll signature (dict sentinel or sig tuple)
        self._watch_sizes: dict[str, int] = {}   # last-seen size, for write-in-progress detection
        self._watch_scanning = False             # a background scan is in flight
        self._watch_interval_ms = 3000           # poll cadence (configurable in Properties)
        self._watch_settle = 2.0                 # seconds a file must be quiet before ingest
        self._watch_ignore = ""                  # a dir to skip while scanning (auto-render output)
        self._targets: list[str] = []            # materials marked as render targets
        self._autorender_last: frozenset | None = None   # last target version-set emitted
        self._watch_idle_polls = 0               # consecutive no-change polls (drives back-off)
        self._grouping_mode = False              # asset-grouping: emit ready clips instead of auto-mapping
        self._build_ui()
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(self._watch_interval_ms)   # poll (robust on network shares)
        self._watch_timer.timeout.connect(self._scan_watch_folder)
        self._watch_scanned.connect(self._apply_watch_scan)
        # Hybrid fast path: a filesystem watcher gives instant pickup for LOCAL
        # folders (no waiting for the next poll), while the poll above stays as
        # the reliable backstop for network shares / cloud folders where native
        # FS events are unreliable or silently dropped.
        self._fs_watcher = QFileSystemWatcher(self)
        self._fs_watcher.directoryChanged.connect(self._on_fs_event)
        self._fs_event_timer = QTimer(self)      # coalesce a burst of FS events
        self._fs_event_timer.setSingleShot(True)
        self._fs_event_timer.setInterval(250)
        self._fs_event_timer.timeout.connect(self._scan_watch_folder)
        # Debounce auto-render: coalesce a burst of new target versions (landing
        # across several polls) into a single render once the set settles.
        self._autorender_timer = QTimer(self)
        self._autorender_timer.setSingleShot(True)
        self._autorender_timer.setInterval(max(4000, 2 * self._watch_interval_ms))
        self._autorender_timer.timeout.connect(self._fire_target_set)
        # Linking a clip auto-targets the material, then re-checks the auto-render
        # set on any mapping change (manual link, auto-map, unmap, watch ingest).
        self.assignments_changed.connect(lambda *_: self._auto_target_and_check())

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
        if event.type() == QEvent.Type.Leave:
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
        self.camera_combo.setToolTip("Camera to render through. Blank = the scene's active camera.")
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
        left_top_w.setFixedHeight(30)   # match the Videos header (+ button) so the
        left.addWidget(left_top_w)       # two filter fields line up vertically
        self.mat_search = QLineEdit()
        self.mat_search.setPlaceholderText("Filter materials")
        self.mat_search.textChanged.connect(self._refresh_lists)
        self.mat_list = MaterialListWidget()
        # The left stripe is the render-target indicator: outline = targeted (no
        # clip yet), colourful = clip linked, ghost on hover = click to target.
        self.mat_list.setItemDelegate(TargetStripeDelegate(self._toggle_target, self, self.mat_list))
        self.mat_list.setMouseTracking(True)
        self.mat_list.viewport().setMouseTracking(True)
        self.mat_list.currentItemChanged.connect(lambda *_: self._update_maplink_btn())
        self.mat_list.currentItemChanged.connect(lambda *_: self._recompute_cross_highlight())
        self.mat_list.itemEntered.connect(self._on_mat_entered)
        self.mat_list.viewport().installEventFilter(self)
        self.mat_list.scene_dropped.connect(self._on_scene_file_dropped)
        # Direct manipulation: drag a clip onto a material to map it; double-click
        # either side of a selected pair to map them.
        self.mat_list.clip_dropped.connect(self._map_pair)
        self.mat_list.itemDoubleClicked.connect(lambda *_: self._map_selected())
        self.mat_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.mat_list.customContextMenuRequested.connect(self._show_material_context_menu)
        # ⌘L links the selected material + clip (Qt maps Ctrl→⌘ on macOS).
        _map_sc = QShortcut(QKeySequence("Ctrl+L"), self)
        _map_sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        _map_sc.activated.connect(self._map_selected)
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
        self.add_video_btn.setFixedSize(28, 28)   # >=24px hit target (WCAG 2.5.8)
        self.add_video_btn.clicked.connect(self._add_videos)
        right_top.addWidget(self.add_video_btn)
        right_top_w = QWidget()
        right_top_w.setLayout(right_top)
        right_top_w.setFixedHeight(30)
        self.vid_search = QLineEdit()
        self.vid_search.setPlaceholderText("Filter videos")
        self.vid_search.textChanged.connect(self._refresh_lists)
        self.vid_list = VideoListWidget()
        self.vid_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.vid_list.viewport().setMouseTracking(True)
        self.vid_list.setItemDelegate(AudioBadgeDelegate(self.toggle_mute, self, self.vid_list))
        self.vid_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.vid_list.customContextMenuRequested.connect(self._show_video_context_menu)
        self.vid_list.currentItemChanged.connect(lambda *_: self._recompute_cross_highlight())
        self.vid_list.itemDoubleClicked.connect(lambda *_: self._map_selected())
        self.vid_list.itemEntered.connect(self._on_vid_entered)
        self.vid_list.viewport().installEventFilter(self)
        self.vid_list.files_dropped.connect(self._add_video_paths)

        remove_video_action = QAction("Remove Selected", self)
        # macOS "delete" key emits Backspace; bind both so it actually fires.
        remove_video_action.setShortcuts([QKeySequence(Qt.Key.Key_Delete), QKeySequence(Qt.Key.Key_Backspace)])
        remove_video_action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
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
        self.watch_btn.setFixedSize(28, 28)
        self.watch_btn.setToolTip("Watch a folder — auto-import new clips and update to the latest version")
        self.watch_btn.toggled.connect(self._on_watch_toggled)
        self.watch_label = QLabel("No watch folder")
        self.watch_label.setObjectName("FieldLabel")
        self.watch_browse_btn = QPushButton("")
        self.watch_browse_btn.setObjectName("IconButton")
        self.watch_browse_btn.setFixedSize(28, 28)
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
            p = local or p[7:]
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
            path = item.data(Qt.ItemDataRole.UserRole) or item.text()
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
        n = len(paths)
        mapped = sum(1 for a in self._assignments if a.video_path in paths)
        msg = f"Remove {n} clip{'s' if n != 1 else ''}?"
        if mapped:
            msg += f"\n\n{mapped} material mapping{'s' if mapped != 1 else ''} will be removed too."
        msg += "\n\nThis can be undone with Ctrl+Z."
        if QMessageBox.question(self, "Remove Clips", msg) != QMessageBox.StandardButton.Yes:
            return
        # Snapshot for undo: removing clips silently cascades to their mappings
        # and mute state, so it must be reversible like the other destructive actions.
        snapshot = {
            "videos": list(self._videos),
            "assignments": [MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode)
                            for a in self._assignments],
            "muted": set(self._muted_videos),
        }
        self.videos_removed.emit(len(paths), snapshot)
        self._videos = [v for v in self._videos if v not in paths]
        self._assignments = [a for a in self._assignments if a.video_path not in paths]
        self._muted_videos -= paths
        self._refresh_lists()
        self.videos_changed.emit(list(self._videos))
        self.assignments_changed.emit(list(self._assignments))

    def restore_videos_snapshot(self, snapshot: dict) -> None:
        """Undo support: restore clips + their mappings + mute state."""
        self._videos = list(snapshot.get("videos", []))
        self._assignments = [MaterialVideoAssignment(a.material_name, a.video_path, a.mapping_mode)
                             for a in snapshot.get("assignments", [])]
        self._muted_videos = set(snapshot.get("muted", set()))
        self._refresh_lists()
        self.videos_changed.emit(list(self._videos))
        self.assignments_changed.emit(list(self._assignments))

    def _map_pair(self, material: str, video: str) -> None:
        """Map a clip onto a material (create or replace its assignment). Shared by
        the link button, double-click, the ⌘L shortcut, and drag-clip-onto-material."""
        material = (material or "").strip()
        video = str(video or "")
        if not material or not video or video.startswith("__add_video__"):
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

    def _map_selected(self) -> None:
        mat_item = self.mat_list.currentItem()
        vid_item = self.vid_list.currentItem()
        if mat_item is None or vid_item is None:
            return
        video = vid_item.data(Qt.ItemDataRole.UserRole) or vid_item.text()
        self._map_pair(mat_item.text(), video)

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
        n = len(self._assignments)
        if QMessageBox.question(
            self, "Clear Mappings",
            f"Remove all {n} mapping{'s' if n != 1 else ''}?\n\nThis can be undone with Ctrl+Z.",
        ) != QMessageBox.StandardButton.Yes:
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
    def _toggle_target(self, mat: str) -> None:
        if mat in self._targets:
            self._targets.remove(mat)
        else:
            self._targets.append(mat)
        self._autorender_last = None     # re-evaluate against the new target set
        self._refresh_lists()
        self.targets_changed.emit(list(self._targets))
        self._check_target_set()

    def set_targets(self, targets: list) -> None:
        self._targets = [str(t) for t in targets if str(t)]
        self._autorender_last = None
        self._refresh_lists()

    def get_targets(self) -> list:
        return list(self._targets)

    def _show_material_context_menu(self, pos) -> None:
        item = self.mat_list.itemAt(pos)
        if item is None:
            return
        mat = item.text()
        menu = QMenu(self)
        act = menu.addAction("Unmark Render Target" if mat in self._targets else "Mark as Render Target")
        act.triggered.connect(lambda: self._toggle_target(mat))
        # Mapping mode — only when the material has a clip (it's a property of the
        # mapping). Honoured on the Blender path; C4D/three.js render emissive.
        assignment = next((a for a in self._assignments if a.material_name == mat), None)
        if assignment is not None:
            menu.addSeparator()
            sub = menu.addMenu("Mapping mode")
            for label, mode in (("Emission (full-bright)", VIDEO_MAPPING_MODE_EMISSION),
                                 ("Base colour (with alpha)", VIDEO_MAPPING_MODE_BASE_COLOR)):
                a2 = sub.addAction(label)
                a2.setCheckable(True)
                a2.setChecked(assignment.mapping_mode == mode)
                a2.triggered.connect(lambda _c=False, m=mode: self._set_mapping_mode(mat, m))
        menu.exec(self.mat_list.mapToGlobal(pos))

    def _set_mapping_mode(self, material: str, mode: str) -> None:
        """Switch how a mapped clip drives its material — full-bright emission
        (default, screen-like) or base colour with alpha. Persists with the job."""
        changed = False
        for i, a in enumerate(self._assignments):
            if a.material_name == material and a.mapping_mode != mode:
                self._assignments[i] = MaterialVideoAssignment(a.material_name, a.video_path, mode)
                changed = True
        if changed:
            self._refresh_lists()
            self.assignments_changed.emit(self.get_assignments())

    def _auto_target_and_check(self) -> None:
        """Linking a clip auto-targets its material, then re-check the set."""
        changed = False
        for a in self._assignments:
            if a.material_name not in self._targets:
                self._targets.append(a.material_name)
                changed = True
        if changed:
            self.targets_changed.emit(list(self._targets))
        self._check_target_set()

    def _target_version_set(self):
        """The (target → clip) map if EVERY target has a clip, else None."""
        if not self._targets:
            return None
        mapped = {a.material_name: a for a in self._assignments}
        if not all(t in mapped for t in self._targets):
            return None
        return mapped

    def _check_target_set(self) -> None:
        """Once every render-target material has a clip, (re)start a short debounce;
        the render is queued only when the set stops changing — so a batch of new
        versions becomes a single render, not one per file."""
        mapped = self._target_version_set()
        if mapped is None:
            self._autorender_timer.stop()       # not every target has a clip yet
            return
        version_set = frozenset((t, mapped[t].video_path) for t in self._targets)
        if version_set == self._autorender_last:
            return                              # already rendered this exact set
        self._autorender_timer.start()          # wait for the set to settle, then fire

    def _fire_target_set(self) -> None:
        mapped = self._target_version_set()
        if mapped is None:
            return
        version_set = frozenset((t, mapped[t].video_path) for t in self._targets)
        if version_set == self._autorender_last:
            return
        self._autorender_last = version_set
        snapshot = [MaterialVideoAssignment(t, mapped[t].video_path, mapped[t].mapping_mode)
                    for t in self._targets]
        self.target_set_ready.emit(snapshot)

    # ── Watch folder ─────────────────────────────────────────────────────
    def _choose_watch_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Choose watch folder",
                                             self._watch_folder or str(Path.home()))
        if d:
            self.set_watch_folder(d, True)
            self.watch_changed.emit(self._watch_folder, True)

    def _start_watching(self) -> None:
        """Begin watching: fresh scan, start the poll backstop at the base cadence
        and arm the FS-event fast path on the folder."""
        self._watch_seen = {}          # force a fresh scan
        self._watch_sizes = {}
        self._reset_poll_interval()
        self._scan_watch_folder()
        self._watch_timer.start()
        self._sync_fs_watcher()

    def _stop_watching(self) -> None:
        self._watch_timer.stop()
        self._fs_event_timer.stop()
        self._sync_fs_watcher()

    def _sync_fs_watcher(self) -> None:
        """Keep the QFileSystemWatcher pointed at the active watch folder (and
        nothing else when watching is off or the folder is gone)."""
        old = self._fs_watcher.directories()
        if old:
            self._fs_watcher.removePaths(old)
        if self.watch_btn.isChecked() and self._watch_folder and os.path.isdir(self._watch_folder):
            self._fs_watcher.addPath(self._watch_folder)

    def _on_fs_event(self, _path: str) -> None:
        """A local change landed — scan almost immediately (debounced 250ms to
        coalesce a burst) and snap the poll cadence back to its base."""
        self._reset_poll_interval()
        self._fs_event_timer.start()

    def _reset_poll_interval(self) -> None:
        self._watch_idle_polls = 0
        if self._watch_timer.interval() != self._watch_interval_ms:
            self._watch_timer.setInterval(self._watch_interval_ms)

    def _note_idle_poll(self) -> None:
        """Back the poll cadence off after sustained quiet (up to ~5x base, 20s
        cap). Safe because the FS watcher still catches local drops instantly —
        the slower poll only delays detection on event-less network shares."""
        self._watch_idle_polls += 1
        steps = min(self._watch_idle_polls // 4, 4)        # 0..4 → 1x..5x
        target = min(self._watch_interval_ms * (1 + steps), 20000)
        if target != self._watch_timer.interval():
            self._watch_timer.setInterval(target)

    def _on_watch_toggled(self, on: bool) -> None:
        self._update_watch_ui()
        if on and self._watch_folder:
            self._start_watching()
        else:
            self._stop_watching()
        self.watch_changed.emit(self._watch_folder, self.watch_btn.isChecked())

    def set_watch_folder(self, folder: str, enabled: bool) -> None:
        """Set (and optionally start) the watch folder — used on profile load."""
        self._watch_folder = folder or ""
        self.watch_btn.blockSignals(True)
        self.watch_btn.setChecked(bool(enabled and self._watch_folder))
        self.watch_btn.blockSignals(False)
        self._update_watch_ui()
        if self.watch_btn.isChecked():
            self._start_watching()
        else:
            self._stop_watching()

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
        self._reset_poll_interval()      # apply the new base cadence now (clears any back-off)
        self._autorender_timer.setInterval(max(4000, 2 * self._watch_interval_ms))

    def get_watch_options(self) -> tuple[int, float]:
        return self._watch_interval_ms, self._watch_settle

    def set_grouping_mode(self, on: bool) -> None:
        """When on, the watch folder emits ready clips for asset-grouping instead
        of auto-mapping them onto the current scene."""
        on = bool(on)
        if on != self._grouping_mode:
            self._grouping_mode = on
            self._watch_seen = {}   # force a fresh emit/scan under the new mode

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
                                # Flag cloud "online-only" placeholders so we don't
                                # ingest a file whose bytes aren't on disk yet.
                                dataless = is_cloud_placeholder(e.path, st)
                                listing.append((e.path, st.st_size, st.st_mtime, dataless))
                        except OSError:
                            _log.debug("watch folder: failed to stat an entry", exc_info=True)
            except OSError:
                listing = []
            self._watch_scanned.emit(listing)   # queued → delivered on the UI thread

        threading.Thread(target=work, daemon=True).start()

    def set_watch_ignore_dir(self, path: str) -> None:
        self._watch_ignore = path or ""

    def _apply_watch_scan(self, listing: list) -> None:
        self._watch_scanning = False
        if not self._watch_folder:
            return
        folder = os.path.normpath(self._watch_folder)
        now = time.time()
        # All listed files count as "present" — including cloud placeholders — so
        # a clip that's evicted to online-only after ingest isn't treated as gone.
        present = {os.path.normpath(p) for p, _s, _m, _d in listing}
        # Clips that came from the watch folder but are gone now (deleted, or
        # renamed away) — drop them so a rename doesn't leave a stale duplicate.
        gone = {v for v in self._videos
                if os.path.normpath(os.path.dirname(v)) == folder and os.path.normpath(v) not in present}

        ready, mtimes, sizes = [], {}, {}
        for path, size, mtime, dataless in listing:
            sizes[path] = size
            # "Ready" = finished copying AND actually on disk: non-empty, not a
            # cloud placeholder, and either its size held steady since the last
            # poll or it hasn't been touched for a while. Avoids ingesting a
            # half-written file mid-copy or a not-yet-downloaded online-only file.
            if (size > 0 and not dataless
                    and (self._watch_sizes.get(path) == size or (now - mtime) >= self._watch_settle)):
                ready.append(path)
                mtimes[path] = mtime
        self._watch_sizes = sizes           # remember sizes for next poll's stability check

        # Asset-grouping mode: don't auto-map onto the current scene — hand the
        # ready clips to the app, which parses the naming convention and builds
        # one previz render job per (setup, asset).
        if self._grouping_mode:
            gsig = tuple(sorted((p, mtimes.get(p, 0.0)) for p in ready))
            if gsig == self._watch_seen:
                self._note_idle_poll()
                return
            self._watch_seen = gsig
            self._reset_poll_interval()
            self.watch_clips_ready.emit(list(ready))
            return

        sig = (dict(mtimes), tuple(sorted(gone)))
        if sig == self._watch_seen:
            self._note_idle_poll()          # quiet → let the poll cadence back off
            return                          # nothing changed since last poll
        self._watch_seen = sig

        base_videos = [v for v in self._videos if v not in gone]
        videos_after, replacements, added = reconcile_versions(base_videos, ready, mtimes)
        if not gone and not replacements and not added:
            self._note_idle_poll()
            return
        self._reset_poll_interval()         # activity → snap back to the fast cadence

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
            targeted = m in self._targets
            if m in mat_to_idx:
                item.setData(ROLE_MAP_COLOR, LINK_COLORS[mat_to_idx[m] % len(LINK_COLORS)])
                item.setToolTip(f"{m}\nRender target — clip linked")
            elif targeted:
                item.setToolTip(f"{m}\nRender target — waiting for a clip")
            else:
                item.setToolTip(f"{m}\nClick the left edge (or right-click) to mark as a render target")
            if targeted:
                item.setData(ROLE_TARGET, True)
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
                if self.vid_list.item(i).data(Qt.ItemDataRole.UserRole) == current_vid:
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
        self.camera_combo.addItems(["", *normalized])
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
        return item.data(Qt.ItemDataRole.UserRole) or ""

    def _show_video_context_menu(self, pos) -> None:
        item = self.vid_list.itemAt(pos)
        menu = QMenu(self)
        add_action = menu.addAction("Add Videos...")
        render_action = menu.addAction("Start Render")
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
        elif mute_action is not None and item is not None and chosen == mute_action:
            self.toggle_mute(item.data(ROLE_VIDEO_PATH))


class RenderPanel(QWidget):
    output_changed = Signal(str)
    tokens_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self) -> None:
        # Scrollable content so this dense settings panel doesn't force a tall
        # minimum window height — the window can shrink to the 70% default.
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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
        root.addWidget(section("RESOLUTION & FPS"))
        res_row = QHBoxLayout()
        res_row.setSpacing(6)
        self.width_edit = QLineEdit("1920")
        self.width_edit.setToolTip("Output width in pixels.")
        self.width_edit.setPlaceholderText("W")
        self.height_edit = QLineEdit("1080")
        self.height_edit.setToolTip("Output height in pixels.")
        self.height_edit.setPlaceholderText("H")
        x_lbl = QLabel("×")
        x_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.fps_edit = QLineEdit("30")
        self.fps_edit.setToolTip("Frames per second of the output (and clip playback).")
        self.fps_edit.setPlaceholderText("FPS")
        fps_lbl = QLabel("fps")
        fps_lbl.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        res_row.addWidget(self.width_edit, 3)
        res_row.addWidget(x_lbl)
        res_row.addWidget(self.height_edit, 3)
        res_row.addSpacing(10)
        res_row.addWidget(self.fps_edit, 2)
        res_row.addWidget(fps_lbl)
        root.addLayout(res_row)

        # ── Frame Range ───────────────────────────────────────────────
        root.addWidget(section("FRAMES"))
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
            lab = QLabel(lbl)
            lab.setObjectName("FieldLabel")
            col.addWidget(lab)
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
        self.engine_combo.setToolTip("Render engine. Cycles = highest quality (slow); EEVEE = fast preview-grade. C4D scenes use Redshift.")
        self.populate_engines(["CYCLES", "BLENDER_EEVEE"])
        renderer_col.addWidget(self.engine_combo)
        # A Premiere/AME-style summary under the picker: a backend-coloured badge
        # plus a one-line description of what the selected engine actually is.
        self.engine_summary = QLabel()
        self.engine_summary.setObjectName("FieldLabel")
        self.engine_summary.setTextFormat(Qt.TextFormat.RichText)
        self.engine_summary.setWordWrap(True)
        renderer_col.addWidget(self.engine_summary)
        self.engine_combo.currentIndexChanged.connect(lambda _i: self._update_engine_summary())

        format_col = QVBoxLayout()
        format_col.setSpacing(2)
        format_col.addWidget(section("MASTER"))
        self.profile_combo = QComboBox()
        self.profile_combo.setToolTip("The master deliverable's container: H264 MP4 for review, ProRes MOV for editorial, PNG/EXR sequences for comp.")
        self.profile_combo.addItems(list(OUTPUT_PROFILES.keys()))
        format_col.addWidget(self.profile_combo)

        # Extra deliverables produced from the SAME render (e.g. a ProRes master
        # plus an H.264 review proxy) — transcoded from the master output, so one
        # render emits multiple formats without re-rendering the 3D scene. The
        # master's own format is never offered here (no 'double MP4'), and formats
        # the active renderer can't produce are hidden — see _sync_deliverables.
        self.extra_output_checks: dict[str, QCheckBox] = {}
        _movie_profiles = [n for n, (fmt, _c) in OUTPUT_PROFILES.items() if fmt in ("MPEG4", "QUICKTIME")]
        self.also_label = QLabel("Also deliver:")
        self.also_label.setObjectName("FieldLabel")
        self.also_label.setToolTip("Extra files transcoded from the master render — no re-render.")
        format_col.addWidget(self.also_label)
        for name in _movie_profiles:
            cb = QCheckBox(name)
            cb.setToolTip(f"Also produce a {name}, transcoded from the master render")
            self.extra_output_checks[name] = cb
            format_col.addWidget(cb)
        self.profile_combo.currentTextChanged.connect(lambda _v: self._sync_deliverables())

        ro_row.addLayout(renderer_col, 1)
        ro_row.addLayout(format_col, 1)
        root.addLayout(ro_row)

        # ── Output Path ───────────────────────────────────────────────
        root.addWidget(section("PATH"))
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


        # ── Quality & output — inline, tailored to the renderer ─────────
        # No more "Advanced" drawer: these settings are always visible, and each
        # section auto-shows only for the renderer(s) it applies to (set_renderer).
        self.adv_box = QWidget()
        adv = QVBoxLayout(self.adv_box)
        adv.setContentsMargins(0, 2, 0, 0)
        adv.setSpacing(6)

        def labeled(text: str, w: QWidget) -> QVBoxLayout:
            """A field label stacked above its input, matching the panel style."""
            col = QVBoxLayout()
            col.setSpacing(3)
            lbl = QLabel(text)
            lbl.setObjectName("FieldLabel")
            col.addWidget(lbl)
            col.addWidget(w)
            return col

        def two_col(left: QVBoxLayout, right: QVBoxLayout | None = None) -> QHBoxLayout:
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
        self.quality_header = section("QUALITY")
        adv.addWidget(self.quality_header)
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
        _tr.addLayout(labeled("Noise threshold", self.rs_threshold_edit))
        adv.addWidget(self.rs_threshold_row)
        # Denoise (both) + transparent (Blender only).
        self.denoise_cb = QCheckBox("Denoise")
        self.denoise_cb.setToolTip("AI-denoise the render — lets you use far fewer samples for the same look.")
        self.denoise_cb.setChecked(True)
        self.transparent_cb = QCheckBox("Transparent (alpha)")
        self.burn_in_cb = QCheckBox("Burn-in overlay")
        self.burn_in_cb.setToolTip("Stamps the clip name/version, frame number, camera and date "
                                   "onto every frame — so reviews always know which version "
                                   "they're looking at. (Farm C4D renders: not yet.)")
        self.transparent_cb.setToolTip("Render with a transparent background — needs PNG/EXR/ProRes output.")
        self.safe_mode_cb = QCheckBox("Validate paths")
        self.safe_mode_cb.setChecked(True)
        self.safe_mode_cb.setToolTip(
            "Before rendering, check that the scene and every mapped clip exist, are "
            "readable, and have a supported extension. Turn off only if you use "
            "symlinks or relative paths that are valid on the render machine.")
        # Stacked vertically (not one wide row) so the long labels never force a
        # huge minimum width — that's what made opening Advanced stretch the dock.
        cb_col = QVBoxLayout()
        cb_col.setSpacing(5)
        cb_col.setContentsMargins(0, 2, 0, 0)
        for _cb in (self.denoise_cb, self.transparent_cb, self.burn_in_cb, self.safe_mode_cb):
            cb_col.addWidget(_cb)
        adv.addLayout(cb_col)

        # ── Output (same slot for both renderers) ────────────────────────
        adv.addWidget(section("ENCODING"))
        self.scale_combo = QComboBox()
        self.scale_combo.setToolTip("Render scale: 50% renders at half resolution — much faster drafts, same framing.")
        self.scale_combo.addItems(["100%", "75%", "50%", "25%"])
        self.quality_combo = QComboBox()
        self.quality_combo.setToolTip("Movie bitrate/quality (H264 only).")
        self.quality_combo.addItems(["Lossless", "High", "Medium", "Low", "Lowest"])
        self.quality_combo.setCurrentText("High")
        adv.addLayout(two_col(
            labeled("Render Scale", self.scale_combo),
            labeled("Video Quality", self.quality_combo),
        ))
        self.codec_combo = QComboBox()
        self.codec_combo.setToolTip("Video codec inside the container.")
        self.codec_combo.addItems(["Default", "H.264", "H.265"])
        self.device_combo = QComboBox()
        self.device_combo.setToolTip("Render device: GPU is much faster when available.")
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
        gi_lay.setSpacing(6)
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
        color_lay.setSpacing(6)
        color_lay.addWidget(section("COLOR"))
        self.view_transform_combo = QComboBox()
        self.view_transform_combo.setToolTip("Colour view transform: AgX/Filmic = cinematic tone-map, Standard = raw sRGB.")
        self.view_transform_combo.addItems(["AgX", "Filmic", "Standard", "Khronos PBR Neutral", "Raw", "False Color"])
        self.view_transform_combo.setCurrentText("AgX")
        color_lay.addLayout(labeled("View Transform", self.view_transform_combo))
        self.exposure_edit = QLineEdit("0.0")
        self.exposure_edit.setToolTip("Colour exposure adjustment in stops (0 = unchanged).")
        self.gamma_edit = QLineEdit("1.0")
        self.gamma_edit.setToolTip("Display gamma adjustment (1.0 = unchanged).")
        color_lay.addLayout(two_col(
            labeled("Exposure", self.exposure_edit),
            labeled("Gamma", self.gamma_edit),
        ))
        adv.addWidget(self.color_box)

        # ── Ambient occlusion (Blender EEVEE/Cycles only) ────────────────
        self.ao_box = QWidget()
        ao_lay = QVBoxLayout(self.ao_box)
        ao_lay.setContentsMargins(0, 0, 0, 0)
        ao_lay.setSpacing(6)
        self.ao_cb = QCheckBox("Ambient occlusion")
        self.ao_cb.setToolTip("Adds soft contact-shadow depth where surfaces meet — "
                              "occludes ambient light in creases. Off keeps the flat look.")
        ao_lay.addWidget(self.ao_cb)
        self.ao_distance_edit = QLineEdit("0.2")
        self.ao_distance_edit.setToolTip("How far occlusion reaches, in world units. Larger = broader, softer.")
        self.ao_factor_edit = QLineEdit("1.0")
        self.ao_factor_edit.setToolTip("Strength (EEVEE-Legacy / Cycles; EEVEE Next uses distance).")
        ao_lay.addLayout(two_col(
            labeled("AO Distance", self.ao_distance_edit),
            labeled("AO Factor", self.ao_factor_edit),
        ))
        adv.addWidget(self.ao_box)

        # ── Scene lighting (web / three.js only) ─────────────────────────
        self.web_light_box = QWidget()
        wl = QVBoxLayout(self.web_light_box)
        wl.setContentsMargins(0, 0, 0, 0)
        wl.setSpacing(6)
        wl.addWidget(section("LIGHTING"))
        self.web_light_preset_combo = QComboBox()
        self.web_light_preset_combo.addItems(["Auto", "Studio", "Outdoor", "Flat", "None"])
        self.web_light_preset_combo.setToolTip(
            "Auto = neutral studio (recommended); Studio = crisp product shot; "
            "Outdoor = warm daylight; Flat = even/shadowless; None = unlit (emissive only).")
        self.web_light_intensity_slider = QSlider(Qt.Orientation.Horizontal)
        self.web_light_intensity_slider.setRange(0, 200)   # 0.0–2.0x
        self.web_light_intensity_slider.setValue(100)
        self.web_light_intensity_val = QLabel("1.0x")
        self.web_light_intensity_slider.valueChanged.connect(
            lambda v: self.web_light_intensity_val.setText(f"{v / 100:.1f}x"))
        self.web_respect_lights_cb = QCheckBox("Use scene lights")
        self.web_respect_lights_cb.setChecked(True)
        self.web_respect_lights_cb.setToolTip(
            "If the .glb ships its own lights, keep the artist's lighting and skip the preset rig.")
        intensity_col = labeled("Intensity", self.web_light_intensity_slider)
        intensity_col.addWidget(self.web_light_intensity_val)
        web_light_row = QHBoxLayout()
        web_light_row.setSpacing(10)
        web_light_row.addLayout(labeled("Preset", self.web_light_preset_combo), 1)
        web_light_row.addLayout(intensity_col, 1)
        wl.addLayout(web_light_row)
        wl.addWidget(self.web_respect_lights_cb)
        adv.addWidget(self.web_light_box)

        # Preset wiring + initial renderer state (Blender layout by default).
        self._rs_applying = False
        self.rs_preset_combo.currentTextChanged.connect(self._apply_rs_preset)
        for w in (self.samples_edit, self.rs_min_samples_edit, self.rs_threshold_edit,
                  self.rs_gi_bounces_edit, self.rs_ray_depth_edit):
            w.textEdited.connect(self._rs_custom)
        self.rs_gi_cb.toggled.connect(lambda _v: self._rs_custom())
        self.set_renderer(False)
        self._update_engine_summary()

        root.addWidget(self.adv_box)

        root.addStretch()
        self.restyle(active_palette())

    # Redshift speed/quality presets — each fills the optimization fields.
    _RS_PRESETS: ClassVar = {
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
            self.samples_edit.setText(str(p["mx"]))
            self.rs_min_samples_edit.setText(str(p["mn"]))
            self.rs_threshold_edit.setText(str(p["thr"]))
            self.rs_gi_bounces_edit.setText(str(p["gib"]))
            self.rs_ray_depth_edit.setText(str(p["depth"]))
            self.rs_gi_cb.setChecked(bool(p["gi"]))
        finally:
            self._rs_applying = False

    def _rs_custom(self) -> None:
        """A manual edit to any optimization field flips the preset to Custom."""
        if self._rs_applying or self.rs_preset_combo.currentText() == "Custom":
            return
        self.rs_preset_combo.blockSignals(True)
        self.rs_preset_combo.setCurrentText("Custom")
        self.rs_preset_combo.blockSignals(False)

    # Friendly renderer names in the UI; the enum value Blender/C4D expects is
    # carried as itemData so configs/profiles keep the real identifier.
    ENGINE_LABELS: ClassVar = {"CYCLES": "Cycles", "BLENDER_EEVEE": "EEVEE", "Redshift": "Redshift",
                               "WEB_THREEJS": "three.js (WebGL)"}
    # Backend key (→ _BACKEND_INFO colour) + a terse blurb, shown under the picker
    # like an Adobe Media Encoder format summary. The coloured dot signals backend.
    ENGINE_DESC: ClassVar = {
        "CYCLES": ("blender", "Path-traced · best quality"),
        "BLENDER_EEVEE": ("blender", "Real-time · fast preview"),
        "Redshift": ("c4d", "GPU · Cinema 4D"),
        "WEB_THREEJS": ("web", "Real-time · WebGL"),
    }

    def _update_engine_summary(self) -> None:
        """Refresh the blurb under the renderer picker for the current pick."""
        val = self.engine_value()
        key, desc = self.ENGINE_DESC.get(val, ("blender", ""))
        color = _BACKEND_INFO.get(key, ("#888888", ""))[0]
        self.engine_summary.setText(
            f'<span style="color:{color};">&#9679;</span>&nbsp;{desc}')

    def populate_engines(self, values: list[str]) -> None:
        self.engine_combo.clear()
        for v in values:
            self.engine_combo.addItem(self.ENGINE_LABELS.get(v, v), v)

    def engine_value(self) -> str:
        """The engine identifier (CYCLES / BLENDER_EEVEE / Redshift), not the label."""
        return str(self.engine_combo.currentData() or self.engine_combo.currentText())

    def set_engine_value(self, value: str) -> None:
        i = self.engine_combo.findData(value)
        if i >= 0:
            self.engine_combo.setCurrentIndex(i)

    def engine_values(self) -> list[str]:
        return [str(self.engine_combo.itemData(i)) for i in range(self.engine_combo.count())]

    def extra_output_profiles(self) -> list[str]:
        """Checked 'also export' profiles, minus the primary (no self-transcode)."""
        primary = self.profile_combo.currentText()
        return [n for n, cb in self.extra_output_checks.items() if cb.isChecked() and n != primary]

    def set_extra_output_profiles(self, profiles: list[str] | None) -> None:
        wanted = set(profiles or [])
        for n, cb in self.extra_output_checks.items():
            cb.setChecked(n in wanted)

    def set_renderer(self, is_c4d: bool, is_web: bool = False) -> None:
        """Adapt the settings to the active renderer so every visible control is
        real. Redshift relabels samples and shows its optimization controls; the
        web/three.js backend hides the Blender-only Device + Color + alpha
        controls (it ignores them) and offers only the outputs it can produce."""
        self.samples_label.setText("Max Samples" if is_c4d else "Cycles Samples")
        self.quality_header.setText(
            "QUALITY · " + ("Redshift" if is_c4d else "three.js" if is_web else "Blender"))
        # Redshift-only sampling/GI controls.
        for w in (self.rs_preset_row, self.rs_min_box, self.rs_threshold_row, self.gi_box):
            w.setVisible(is_c4d)
        # Blender-only controls — also hidden for the web/three.js backend.
        self.device_box.setVisible(not is_c4d and not is_web)   # Redshift/web are GPU-only
        self.color_box.setVisible(not is_c4d and not is_web)    # Blender color management
        self.ao_box.setVisible(not is_c4d and not is_web)       # Blender EEVEE/Cycles AO
        # Samples + denoise are path-tracer concepts — not three.js (no path tracing).
        self.samples_label.setVisible(not is_web)
        self.samples_edit.setVisible(not is_web)
        self.denoise_cb.setVisible(not is_web)
        # Transparent background works in three.js too now (only Redshift hides it —
        # it has its own alpha handling); burn-in works on every backend.
        self.transparent_cb.setVisible(not is_c4d)
        self.web_light_box.setVisible(is_web)                    # three.js scene lighting
        if is_web:
            items = ["H264 MP4", "PNG Sequence"]
        elif is_c4d:
            items = ["H264 MP4", "ProRes MOV", "PNG Sequence"]
        else:
            items = list(OUTPUT_PROFILES.keys())
        existing = [self.profile_combo.itemText(i) for i in range(self.profile_combo.count())]
        if existing != items:
            cur = self.profile_combo.currentText()
            self.profile_combo.blockSignals(True)
            self.profile_combo.clear()
            self.profile_combo.addItems(items)
            idx = self.profile_combo.findText(cur)
            self.profile_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.profile_combo.blockSignals(False)
        self._sync_deliverables()

    def _sync_deliverables(self) -> None:
        """Show an 'also deliver' checkbox only for movie formats that are (a) valid
        for the current renderer and (b) not already the master — so the master's
        own format never appears twice ('double MP4'). Hidden boxes are unchecked,
        and the heading hides when no extra deliverable is possible."""
        primary = self.profile_combo.currentText()
        available = {self.profile_combo.itemText(i) for i in range(self.profile_combo.count())}
        any_visible = False
        for name, cb in self.extra_output_checks.items():
            show = name in available and name != primary
            cb.setVisible(show)
            if not show and cb.isChecked():
                cb.setChecked(False)
            any_visible = any_visible or show
        self.also_label.setVisible(any_visible)

    def restyle(self, pal: T.Palette) -> None:
        self.browse_out_btn.setIcon(icons.icon("folder", pal.text))
        self.open_out_btn.setIcon(icons.icon("open", pal.text))
        self.tokens_btn.setIcon(icons.icon("chevron_down", pal.text))

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
            _log.warning("could not open output folder in file manager", exc_info=True)

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
            self.set_engine_value(target)
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
            engine=self.engine_value(),
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
            burn_in=self.burn_in_cb.isChecked(),
            video_quality=quality_map.get(self.quality_combo.currentText(), "HIGH"),
            video_codec=codec_map.get(self.codec_combo.currentText(), ""),
            rs_min_samples=to_int(self.rs_min_samples_edit.text(), 4),
            rs_threshold=to_float(self.rs_threshold_edit.text(), 0.01),
            rs_gi_enabled=self.rs_gi_cb.isChecked(),
            rs_gi_bounces=to_int(self.rs_gi_bounces_edit.text(), 3),
            rs_ray_depth=to_int(self.rs_ray_depth_edit.text(), 6),
            web_lighting_preset={"Auto": "auto", "Studio": "studio", "Outdoor": "outdoor",
                                 "Flat": "flat", "None": "none"}.get(
                self.web_light_preset_combo.currentText(), "auto"),
            web_lighting_intensity=self.web_light_intensity_slider.value() / 100.0,
            web_respect_scene_lights=self.web_respect_lights_cb.isChecked(),
            ao_enabled=self.ao_cb.isChecked(),
            ao_distance=to_float(self.ao_distance_edit.text(), 0.2),
            ao_factor=to_float(self.ao_factor_edit.text(), 1.0),
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
            self.set_engine_value(str(d["engine"]))
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
        if "burn_in" in d:
            self.burn_in_cb.setChecked(bool(d["burn_in"]))
        if "device" in d:
            self.device_combo.setCurrentText({"AUTO": "Auto", "GPU": "GPU", "CPU": "CPU"}.get(str(d["device"]).upper(), "Auto"))
        if "resolution_percentage" in d:
            try:
                self.scale_combo.setCurrentText(f"{int(d['resolution_percentage'])}%")
            except Exception:
                _log.debug("could not apply resolution_percentage setting", exc_info=True)
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
        if "web_lighting_preset" in d:
            self.web_light_preset_combo.setCurrentText(
                {"auto": "Auto", "studio": "Studio", "outdoor": "Outdoor",
                 "flat": "Flat", "none": "None"}.get(str(d["web_lighting_preset"]), "Auto"))
        if "web_lighting_intensity" in d:
            try:
                self.web_light_intensity_slider.setValue(round(float(d["web_lighting_intensity"]) * 100))
            except Exception:
                _log.debug("could not apply web_lighting_intensity setting", exc_info=True)
        if "web_respect_scene_lights" in d:
            self.web_respect_lights_cb.setChecked(bool(d["web_respect_scene_lights"]))
        if "ao_enabled" in d:
            self.ao_cb.setChecked(bool(d["ao_enabled"]))
        setnum(self.ao_distance_edit, "ao_distance")
        setnum(self.ao_factor_edit, "ao_factor")


class DeadlinePanel(QWidget):
    settings_changed = Signal()
    test_connection_requested = Signal()
    export_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()

    def _on_changed(self, *_) -> None:
        self.settings_changed.emit()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
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

        # Connection state lives right under the toggle so test results are
        # actually visible (it used to update a widget that was never shown).
        self.connection_status_lbl.setText("Not connected — enable to test, or use Deadline → Test Connection")
        self.connection_status_lbl.setWordWrap(True)
        self.connection_status_lbl.setStyleSheet(f"color: {active_palette().text_faint}; font-size: 11px;")
        root.addWidget(self.connection_status_lbl)

        # Container Widget for all other settings
        self.container = QWidget()
        container_layout = QVBoxLayout(self.container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(6)

        # Pools & Groups
        container_layout.addWidget(section("RENDER TARGETS & POOLS"))
        pools_layout = QFormLayout()
        pools_layout.setSpacing(6)
        pools_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)

        self.dl_pool_combo = QComboBox()
        self.dl_pool_combo.setEditable(True)
        if (_ple := self.dl_pool_combo.lineEdit()) is not None:
            _ple.textChanged.connect(self._on_changed)
        pools_layout.addRow("Primary Pool:", self.dl_pool_combo)

        self.dl_group_combo = QComboBox()
        self.dl_group_combo.setEditable(True)
        if (_gle := self.dl_group_combo.lineEdit()) is not None:
            _gle.textChanged.connect(self._on_changed)
        pools_layout.addRow("Group:", self.dl_group_combo)

        container_layout.addLayout(pools_layout)

        # Manual Machine Selection
        container_layout.addWidget(section("MANUAL MACHINE SELECTION"))
        self.dl_machines_list = HintListWidget(
            "Machine list loads from the farm.\nUse Deadline → Test Connection to fetch it.")
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

        self.dl_chunk_strategy = QComboBox()
        self.dl_chunk_strategy.addItems(
            ["Manual", "Auto · ~5 min/task", "Auto · ~10 min/task", "Auto · ~20 min/task"])
        self.dl_chunk_strategy.setToolTip(
            "Auto sizes Frames Per Task from your render history so each task runs "
            "about the chosen time. Falls back to the manual value when there's no "
            "timing history for the scene yet.")
        self.dl_chunk_strategy.currentIndexChanged.connect(self._on_changed)
        ctrl_layout.addRow("Chunking:", self.dl_chunk_strategy)

        self.dl_chunk_spin = QSpinBox()
        self.dl_chunk_spin.setRange(1, 10000)
        self.dl_chunk_spin.setValue(1)
        self.dl_chunk_spin.setToolTip("How many frames rendered by one machine per task "
                                      "(used directly when Chunking = Manual)")
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
            self.dl_machines_list.item(i).setCheckState(Qt.CheckState.Checked)
        self.dl_machines_list.blockSignals(False)
        self._on_changed()

    def _clear_all_machines(self) -> None:
        self.dl_machines_list.blockSignals(True)
        for i in range(self.dl_machines_list.count()):
            self.dl_machines_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.dl_machines_list.blockSignals(False)
        self._on_changed()

    def chunk_target_minutes(self) -> float:
        """Target minutes-per-task for Auto chunking; 0.0 means Manual."""
        return {1: 5.0, 2: 10.0, 3: 20.0}.get(self.dl_chunk_strategy.currentIndex(), 0.0)

    def get_selected_machines(self) -> str:
        selected = []
        for i in range(self.dl_machines_list.count()):
            item = self.dl_machines_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
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
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                self.dl_machines_list.addItem(item)
                existing[name] = item

        # Set check states
        for name, item in existing.items():
            if name in allowed:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)

        self.dl_machines_list.blockSignals(False)


# A 3-backend app should say which renderer each queued job uses at a glance.
_BACKEND_INFO = {
    "blender": ("#4a90d9", "Blender"),
    "c4d": ("#e0533d", "Cinema 4D · Redshift"),
    "web": ("#3fb950", "three.js (WebGL)"),
}
_backend_badge_cache: dict[str, QIcon] = {}


def _job_backend(j: RenderJob) -> str:
    """Which renderer a queued job will use — derived the same way the dispatcher
    decides (web for a .glb on three.js, C4D for .c4d, Blender otherwise)."""
    scene = j.scene_path or ""
    engine = j.render_options.engine if j.render_options else ""
    if is_c4d_scene(scene):
        return "c4d"
    if uses_web_backend(scene, engine):
        return "web"
    return "blender"


def _backend_badge(kind: str) -> QIcon:
    """A small colored rounded-square icon for a backend, cached per kind."""
    if kind not in _backend_badge_cache:
        color = _BACKEND_INFO.get(kind, ("#888", ""))[0]
        pm = QPixmap(12, 12)
        pm.fill(Qt.GlobalColor.transparent)
        pr = QPainter(pm)
        pr.setRenderHint(QPainter.RenderHint.Antialiasing)
        pr.setBrush(QColor(color))
        pr.setPen(Qt.PenStyle.NoPen)
        pr.drawRoundedRect(1, 1, 10, 10, 3, 3)
        pr.end()
        _backend_badge_cache[kind] = QIcon(pm)
    return _backend_badge_cache[kind]


_THUMB_DIR = Path(tempfile.gettempdir()) / "rmp_thumbs"
_THUMB_W, _THUMB_H = 58, 34


def _clip_duration_seconds(clip: str) -> float:
    """Clip duration via ffprobe (0.0 if unknown). Off-thread only."""
    ffprobe = find_ffmpeg_tool("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", clip],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess_creation_flags()).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0


def _extract_thumb_frame(clip: str) -> str:
    """A representative-frame PNG for a clip — the MIDPOINT, not frame 0 (first
    frames are often black / a fade-in / a slate). Cached on disk by path+mtime
    ('' if none). Runs ffmpeg/ffprobe, so call it OFF the UI thread."""
    try:
        p = Path(clip)
        if not p.exists():
            return ""
        if p.suffix.lower() in IMAGE_MEDIA_EXTENSIONS:
            return clip   # an image is its own thumbnail
        ff = find_ffmpeg_tool("ffmpeg")
        if not ff:
            return ""
        _THUMB_DIR.mkdir(parents=True, exist_ok=True)
        # ':mid' invalidates the old first-frame caches from before this change.
        key = hashlib.md5(f"{p.resolve()}:{p.stat().st_mtime_ns}:mid".encode()).hexdigest()[:16]
        out = _THUMB_DIR / f"{key}.png"
        if out.exists() and out.stat().st_size > 0:
            return str(out)
        # Seek to ~50% of the clip for a representative frame; fall back to the 1s
        # mark when the duration can't be probed (still avoids a black frame 0).
        dur = _clip_duration_seconds(clip)
        seek = dur * 0.5 if dur > 0 else 1.0
        subprocess.run([ff, "-y", "-loglevel", "error", "-ss", f"{seek:.3f}", "-i", clip,
                        "-frames:v", "1", "-vf", "scale=120:-1", str(out)],
                       capture_output=True, timeout=20, creationflags=subprocess_creation_flags())
        return str(out) if out.exists() and out.stat().st_size > 0 else ""
    except Exception:
        return ""


class _ThumbnailLoader(QObject):
    """Extracts clip first-frames on background threads and signals when each is
    ready, so the queue fills in posters without blocking the UI."""

    ready = Signal(str, str)   # (clip_path, png_path)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._inflight: set[str] = set()

    def request(self, clip: str) -> None:
        if not clip or clip in self._inflight:
            return
        self._inflight.add(clip)

        def work() -> None:
            png = _extract_thumb_frame(clip)
            self._inflight.discard(clip)
            if png:
                self.ready.emit(clip, png)   # queued back to the UI thread
        threading.Thread(target=work, daemon=True).start()


def _compose_job_icon(png_path: str, kind: str) -> QIcon:
    """A queue-row icon: the clip's first frame (or a neutral tile) with the backend
    badge in the corner — one icon carrying both poster and renderer."""
    pm = QPixmap(_THUMB_W, _THUMB_H)
    pm.fill(QColor("#2a2f39"))
    pr = QPainter(pm)
    pr.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    src = QPixmap(png_path) if png_path else QPixmap()
    if not src.isNull():
        scaled = src.scaled(QSize(_THUMB_W, _THUMB_H), Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                            Qt.TransformationMode.SmoothTransformation)
        pr.drawPixmap((_THUMB_W - scaled.width()) // 2, (_THUMB_H - scaled.height()) // 2, scaled)
    color = _BACKEND_INFO.get(kind, ("#888", ""))[0]
    pr.setBrush(QColor(color))
    pr.setPen(QColor(0, 0, 0, 150))
    pr.drawRoundedRect(_THUMB_W - 13, _THUMB_H - 11, 11, 9, 2, 2)
    pr.end()
    return QIcon(pm)


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
    show_error_requested = Signal(int)
    open_output_requested = Signal(int)
    move_job_requested = Signal(int, int)  # job_id, delta (-1 up / +1 down)
    set_priority_requested = Signal(object)   # job_ids → window prompts for value
    requeue_requested = Signal(object)        # job_ids → reset to idle + selected

    def __init__(self, parent: QWidget | None = None) -> None:
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
        self.table = HintTableWidget(
            0, 7, "Queue is empty.\n\n1. Choose a scene and click Scan\n"
            "2. Add video clips\n3. Map a clip to a material\n\n"
            "A render job then appears here automatically.")
        self.table.setHorizontalHeaderLabels(
            ["Run", "Job", "Preset", "Status", "ETA", "Progress", "Output"])
        run_header = self.table.horizontalHeaderItem(0)
        if run_header is not None:
            run_header.setToolTip("Checked jobs are included when you press Start.")
        eta_header = self.table.horizontalHeaderItem(4)
        if eta_header is not None:
            eta_header.setToolTip("Estimated time: remaining for the running job, "
                                  "or a prediction from past runs of the same scene.")
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setColumnWidth(0, 38)
        self.table.setColumnWidth(2, 112)
        self.table.setColumnWidth(3, 62)
        self.table.setColumnWidth(4, 82)    # ETA
        self.table.setColumnWidth(5, 110)   # Progress
        self.table.verticalHeader().setDefaultSectionSize(40)   # room for a poster thumbnail
        self.table.setIconSize(QSize(_THUMB_W, _THUMB_H))
        self.table.setColumnWidth(1, 220)   # Job cell: thumbnail + name
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._emit_job_selected)
        # Async first-frame thumbnails: requested per clip in set_jobs, filled in
        # (debounced) when ready so the queue reads like a contact sheet.
        self._thumb_png: dict[str, str] = {}
        self._last_jobs: list[RenderJob] = []
        self._last_etas: dict[int, str] = {}
        self._thumb_loader = _ThumbnailLoader(self)
        self._thumb_loader.ready.connect(self._on_thumb_ready)
        self._thumb_refresh = QTimer(self)
        self._thumb_refresh.setSingleShot(True)
        self._thumb_refresh.setInterval(120)
        self._thumb_refresh.timeout.connect(lambda: self.set_jobs(self._last_jobs, self._last_etas))
        self.table.itemChanged.connect(self._on_item_changed)
        # Double-click the Job name to rename it inline.
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.setAlternatingRowColors(True)
        lay.addWidget(self.table)

        # Delete selected rows with Delete/Backspace; duplicate with Cmd/Ctrl+D.
        del_act = QAction("Delete Selected", self.table)
        del_act.setShortcuts([QKeySequence(Qt.Key.Key_Delete), QKeySequence(Qt.Key.Key_Backspace)])
        del_act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        del_act.triggered.connect(self.remove_selected_requested.emit)
        self.table.addAction(del_act)
        dup_act = QAction("Duplicate Selected", self.table)
        dup_act.setShortcut(QKeySequence("Ctrl+D"))
        dup_act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
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

    def _on_thumb_ready(self, clip: str, png: str) -> None:
        """A clip's first-frame finished extracting — cache it and debounce a queue
        refresh so the poster appears (coalesces a burst of thumbnails into one rebuild)."""
        if png and self._thumb_png.get(clip) != png:
            self._thumb_png[clip] = png
            self._thumb_refresh.start()

    def set_jobs(self, jobs: list[RenderJob], etas: dict[int, str] | None = None) -> None:
        self._last_jobs, self._last_etas = list(jobs), dict(etas or {})
        self._failed_ids = {j.id for j in jobs if j.status == "failed"}
        self._finished_ids = {j.id for j in jobs if j.status in ("failed", "cancelled", "success")}
        pal = active_palette()
        faint = QColor(pal.text_faint)
        muted = QColor(pal.text_muted)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for j in jobs:
            r = self.table.rowCount()
            self.table.insertRow(r)
            done = j.status == "success"
            run_item = QTableWidgetItem()
            run_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            run_item.setCheckState(Qt.CheckState.Checked if j.selected else Qt.CheckState.Unchecked)
            run_item.setData(Qt.ItemDataRole.UserRole, j.id)
            if done:
                run_item.setToolTip("Completed — re-check to render again")
            self.table.setItem(r, 0, run_item)
            job_item = QTableWidgetItem(j.label or Path(j.video_path).name or f"Job {j.id}")
            # Only the Job-name cell is editable (double-click to rename); it
            # carries the job id so the rename can be routed back.
            job_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsEditable)
            job_item.setData(Qt.ItemDataRole.UserRole, j.id)
            # Poster thumbnail (clip first frame) + backend badge corner, so a mixed
            # Blender/C4D/three.js queue reads like a contact sheet. The thumbnail
            # loads async; until it's ready the icon is the neutral tile + badge.
            _bk = _job_backend(j)
            _clip = (j.material_assignments[0].video_path if j.material_assignments else j.video_path) or ""
            job_item.setIcon(_compose_job_icon(self._thumb_png.get(_clip, ""), _bk))
            if _clip and _clip not in self._thumb_png:
                self._thumb_loader.request(_clip)
            job_item.setToolTip(f"{_BACKEND_INFO[_bk][1]} · double-click to rename")
            self.table.setItem(r, 1, job_item)
            prof_item = QTableWidgetItem(j.output_profile or "H264 MP4")
            prof_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
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
            eta_item = QTableWidgetItem((etas or {}).get(j.id, ""))
            eta_item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            eta_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            eta_item.setForeground(muted)
            self.table.setItem(r, 4, eta_item)
            self.table.setCellWidget(r, 5, self._make_progress_cell(j))
            out_item = QTableWidgetItem(j.output_path)
            if j.output_path:
                out_item.setToolTip(j.output_path)
            self.table.setItem(r, 6, out_item)
            # Completed jobs dim out (Media-Encoder style); re-checking re-activates.
            if done:
                for c in (1, 2, 3, 4, 6):
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
        bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
            if item and item.checkState() == Qt.CheckState.Checked:
                jid = item.data(Qt.ItemDataRole.UserRole)
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
        jid = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(jid, int):
            self.job_selected.emit(jid)

    def select_job(self, job_id: int) -> None:
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == job_id:
                self.table.setCurrentCell(r, 1)
                break

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        if col == 1:
            item = self.table.item(row, 1)
            if item is not None:
                self.table.editItem(item)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            jid = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(jid, int):
                self.job_run_toggled.emit(jid, item.checkState() == Qt.CheckState.Checked)
        elif item.column() == 1:
            jid = item.data(Qt.ItemDataRole.UserRole)
            name = item.text().strip()
            if isinstance(jid, int) and name:
                self.job_renamed.emit(jid, name)

    def _selected_row_job_ids(self) -> list[int]:
        ids: list[int] = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if not item:
                continue
            jid = item.data(Qt.ItemDataRole.UserRole)
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
        dup_action = reveal_action = open_action = up_action = down_action = delete_action = error_action = None
        prio_action = requeue_action = None
        if selected_ids:
            first = selected_ids[0]
            dup_action = menu.addAction(f"Duplicate  ({MOD_LABEL}D)")
            if first in getattr(self, "_failed_ids", set()):
                error_action = menu.addAction("Show Error…")
            prio_action = menu.addAction("Set Priority…")
            if any(i in getattr(self, "_finished_ids", set()) for i in selected_ids):
                requeue_action = menu.addAction("Requeue (run again)")
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
        elif error_action is not None and action == error_action:
            self.show_error_requested.emit(first)
        elif prio_action is not None and action == prio_action:
            self.set_priority_requested.emit(selected_ids)
        elif requeue_action is not None and action == requeue_action:
            self.requeue_requested.emit(selected_ids)
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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        self.list = HintListWidget(
            "No presets yet.\nDial in render settings, then click Save to keep them as a reusable preset.")
        self.list.itemDoubleClicked.connect(self._load_current)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
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
            item.setData(Qt.ItemDataRole.UserRole, {"path": str(p), "name": p.stem})
            self.list.addItem(item)

    def _load_current(self, _item: QListWidgetItem | None = None) -> None:
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

    def _current_entry(self) -> object | None:
        item = self.list.currentItem()
        if not item:
            return None
        return item.data(Qt.ItemDataRole.UserRole)


class LogsPanel(QWidget):
    copy_diag = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(4)

        # Full history of every line; the view shows everything (detailed),
        # narrowed live by the text filter and the level selector.
        self._raw: list[str] = []
        self._filter_text = ""
        self._level = "All"
        # Active "progress" line: consecutive ticks of one task (a download, a
        # render, …) collapse onto a single in-place updating bar instead of
        # spamming a line each tick. _progress_key is the task id (numbers
        # stripped, so it's stable across ticks); it must index the LAST _raw row.
        self._progress_key: str | None = None
        self._progress_idx = -1

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(6)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter logs…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._on_filter_text)
        self._filter_icon = self.filter_edit.addAction(QIcon(), QLineEdit.ActionPosition.LeadingPosition)
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
        _mono.setStyleHint(QFont.StyleHint.Monospace)   # guaranteed monospace fallback on any OS
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
        if "█" in line or "░" in line:          # a live progress bar
            return pal.success if " 100%" in line else pal.accent
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

    # ── progress lines ──────────────────────────────────────────────────────
    _PROGRESS_RE = re.compile(r"(\d{1,3})\s*%|(\d+)\s*/\s*(\d+)")
    _PROGRESS_HINT = re.compile(
        r"\b(download|downloading|render|rendering|extract|extracting|install|"
        r"installing|fetch|fetching|copy|copying|upload|uploading|frame|sample|"
        r"baking|progress|chromium|runtime)\b", re.I)
    _TS_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s*")

    @classmethod
    def _parse_progress(cls, line: str):
        """``(pct, label, detail, key)`` if ``line`` is a progress tick, else
        ``None``. ``key`` has its numbers stripped so successive ticks of the
        same task share one id and collapse onto a single updating bar."""
        if not cls._PROGRESS_HINT.search(line):
            return None
        for m in cls._PROGRESS_RE.finditer(line):
            if m.start() and line[m.start() - 1] == "=":   # skip scale=50% / frame=12
                continue
            if m.group(1) is not None:
                pct = int(m.group(1))
            else:
                done, tot = int(m.group(2)), int(m.group(3))
                if tot <= 0:
                    continue
                pct = round(100 * done / tot)
            pct = max(0, min(100, pct))
            label = line[:m.start()].rstrip(" -—·:|")
            detail = line[m.end():].strip(" -—·|")
            key = cls._TS_RE.sub("", label).lower()
            key = re.sub(r"[\d.]+", "", key)               # numbers out → stable id
            key = re.sub(r"\s+", " ", key).strip(" |·—-:")
            if not key:
                continue
            return pct, label, detail, key
        return None

    @staticmethod
    def _bar(pct: int, width: int = 16) -> str:
        filled = round(pct / 100.0 * width)
        return "█" * filled + "░" * (width - filled)

    @classmethod
    def _format_progress(cls, label: str, pct: int, detail: str) -> str:
        s = f"{label}  {cls._bar(pct)} {pct:>3d}%"
        return f"{s}  {detail}" if detail else s

    def _replace_last(self, line: str) -> None:
        """Rewrite the last shown row in place (the updating progress bar). If
        the row is filtered out it isn't in the view, so just skip the redraw —
        ``_raw`` is already current and a later re-render will pick it up."""
        import html
        if not self._passes(line):
            return
        color = self._line_color(line, active_palette())
        safe = html.escape(line).replace(" ", "&nbsp;")
        cur = self.text.textCursor()
        cur.movePosition(QTextCursor.MoveOperation.End)
        cur.movePosition(QTextCursor.MoveOperation.StartOfBlock, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        cur.insertHtml(f'<span style="color:{color}; white-space:pre;">{safe}</span>')

    def append(self, line: str) -> None:
        prog = self._parse_progress(line)
        if prog is not None:
            pct, label, detail, key = prog
            bar = self._format_progress(label, pct, detail)
            # Same task as the current bar (and it's still the last row)? Update
            # it in place rather than adding a new line.
            if key == self._progress_key and self._progress_idx == len(self._raw) - 1:
                self._raw[self._progress_idx] = bar
                self._replace_last(bar)
                return
            self._raw.append(bar)
            self._progress_key, self._progress_idx = key, len(self._raw) - 1
            self._emit(bar)
            self._cap()
            return
        # A normal line ends any active progress run.
        self._progress_key = None
        self._raw.append(line)
        self._emit(line)
        self._cap()

    def _cap(self) -> None:
        if len(self._raw) > 8000:          # cap memory for very long sessions
            self._raw = self._raw[-6000:]
            self._progress_key = None      # slice invalidated the index
            self._rerender()

    def _clear(self) -> None:
        self._raw.clear()
        self._progress_key = None
        self._progress_idx = -1
        self.text.clear()


class _PreviewImage(QLabel):
    """Paints its image scaled-to-fit (Fit) or at 1:1 pixels (100%). Double-click
    toggles between the two; at 100% it can be grabbed and panned. Falls back to
    placeholder text when there is no image."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._src: QPixmap | None = None
        self._scroll: QScrollArea | None = None   # set by the panel
        self._on_toggle: Callable | None = None      # callback(QPointF) on dbl-click
        self._pannable = False
        self._panning = False
        self._pan_anchor = None
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(120)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)

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
            self.setCursor(Qt.CursorShape.OpenHandCursor if on else Qt.CursorShape.ArrowCursor)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if self._src is not None and not self._src.isNull() and self._on_toggle:
            self._on_toggle(event.position())

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if self._pannable and event.button() == Qt.MouseButton.LeftButton and self._scroll is not None:
            self._panning = True
            self._pan_anchor = event.globalPosition().toPoint()
            self._h0 = self._scroll.horizontalScrollBar().value()
            self._v0 = self._scroll.verticalScrollBar().value()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
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
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._pannable else Qt.CursorShape.ArrowCursor)
        else:
            super().mouseReleaseEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        if self._src is None or self._src.isNull():
            super().paintEvent(event)   # placeholder text
            return
        scaled = self._src.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(x, y, scaled)
        painter.end()


class PreviewPanel(QWidget):
    """Shows live rendered frames during a render, then plays the finished
    output video itself (looped) when the render completes."""

    preview_frame_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
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
        self.scale_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        head.addWidget(self.scale_combo)
        self.auto_btn = QPushButton()
        self.auto_btn.setObjectName("IconButton")
        self.auto_btn.setFixedSize(34, 28)
        self.auto_btn.setCheckable(True)
        self.auto_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.auto_btn.setToolTip("Auto-render: re-render the preview whenever you change a setting or scrub")
        self.auto_btn.toggled.connect(self._on_auto_toggled)
        head.addWidget(self.auto_btn)
        self.preview_frame_btn = QPushButton()
        self.preview_frame_btn.setObjectName("IconButton")
        self.preview_frame_btn.setFixedSize(34, 28)
        self.preview_frame_btn.setCursor(Qt.CursorShape.PointingHandCursor)
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
            b.setCursor(Qt.CursorShape.PointingHandCursor)
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
        self.frame_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_slider.setMinimum(1)
        self.frame_slider.setMaximum(1)
        self.frame_slider.setToolTip("Drag to pick the frame to preview")
        self.frame_spin = QSpinBox()
        self.frame_spin.setObjectName("FrameSpin")
        self.frame_spin.setMinimum(1)
        self.frame_spin.setMaximum(1)
        self.frame_spin.setFixedWidth(66)
        self.frame_spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.frame_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
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

        self._pixmap: QPixmap | None = None
        self.image_label = _PreviewImage("No preview yet.\nClick the camera button above to render the current frame.")
        self.image_label.setObjectName("HintLabel")
        self.image_label.setToolTip("Double-click to toggle Fit ⇄ 100% · drag to pan at 100%")
        # Scroll area lets fixed-zoom previews (100%, 200%…) pan; in Fit mode the
        # label is resized to the viewport and scaled to fit.
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.image_label)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.image_label._scroll = self.scroll_area
        self.image_label._on_toggle = self._toggle_zoom
        self._zoom_100 = False

        self._has_video = _HAS_MULTIMEDIA
        if self._has_video:
            self.stack = QStackedWidget()
            frame_wrap = QWidget()
            fl = QVBoxLayout(frame_wrap)
            fl.setContentsMargins(0, 0, 0, 0)
            fl.addWidget(self.scroll_area)
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
                self.player.setLoops(QMediaPlayer.Loops.Infinite)
            except Exception:
                self.player.mediaStatusChanged.connect(self._loop_if_ended)

            ctrl = QHBoxLayout()
            ctrl.setContentsMargins(0, 0, 0, 0)
            self.play_btn = QPushButton("Pause")
            self.play_btn.setObjectName("SmallButton")
            self.play_btn.clicked.connect(self._toggle_play)
            self.play_btn.setToolTip("Play/pause the rendered movie (Space)")
            space_act = QAction("Play/Pause", self)
            space_act.setShortcut(QKeySequence(Qt.Key.Key_Space))
            space_act.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            space_act.triggered.connect(self._toggle_play)
            self.addAction(space_act)
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
            lay.addWidget(self.scroll_area, 1)

        # A thin progress strip pinned under the preview. It's ALWAYS in the
        # layout (it just paints transparent when idle), so a render starting or
        # finishing never shifts the preview image up or down.
        self._bar_active = False
        self.render_bar = QProgressBar()
        self.render_bar.setObjectName("PreviewProgress")
        self.render_bar.setTextVisible(False)
        self.render_bar.setFixedHeight(3)
        self.render_bar.setRange(0, 100)
        self.render_bar.setValue(0)
        lay.addWidget(self.render_bar)

        self.auto_btn.setChecked(True)   # auto-render on by default
        self.restyle(active_palette())

    # ── preview render progress (thin bar under the frame) ──────────────────
    def start_render_progress(self) -> None:
        self.render_bar.setRange(0, 0)        # busy/indeterminate until a % lands
        self._set_bar_active(True)

    def set_render_progress(self, pct: int) -> None:
        # A real 0–100 fill once the engine reports a percentage.
        self.render_bar.setRange(0, 100)
        self.render_bar.setValue(max(0, min(100, pct)))
        self._set_bar_active(True)

    def end_render_progress(self) -> None:
        self.render_bar.setRange(0, 100)
        self.render_bar.setValue(0)
        self._set_bar_active(False)

    def _set_bar_active(self, active: bool) -> None:
        """Show/hide the bar by paint only (transparent ⇄ accent), never by
        layout — so the preview never jumps when a render starts or ends."""
        self._bar_active = active
        pal = active_palette()
        if active:
            self.render_bar.setStyleSheet(
                f"QProgressBar#PreviewProgress{{background:{pal.surface_alt};border:none;}}"
                f"QProgressBar#PreviewProgress::chunk{{background:{pal.accent};}}")
        else:
            self.render_bar.setStyleSheet(
                "QProgressBar#PreviewProgress{background:transparent;border:none;}"
                "QProgressBar#PreviewProgress::chunk{background:transparent;}")

    # ── frame picker ─────────────────────────────────────────────────────
    def restyle(self, palette) -> None:
        """Re-tint the scrubber icons for the active palette (called on theme
        change, and once at construction)."""
        self.frame_icon.setPixmap(icons.pixmap("film", palette.text_muted, 16))
        self.prev_btn.setIcon(icons.icon("chevron_left", palette.text, 16))
        self.next_btn.setIcon(icons.icon("chevron_right", palette.text, 16))
        self.preview_frame_btn.setIcon(icons.icon("camera", palette.text, 16))
        self._set_bar_active(self._bar_active)   # keep current state, new palette
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
        self.scroll_area.setWidgetResizable(True)
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
        self.scroll_area.setWidgetResizable(False)
        self.image_label.set_fixed(iw, ih)
        self.image_label.set_pannable(True)

        def _center():
            vp = self.scroll_area.viewport()
            self.scroll_area.horizontalScrollBar().setValue(int(cx - vp.width() / 2))
            self.scroll_area.verticalScrollBar().setValue(int(cy - vp.height() / 2))
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
        self.image_label.setText("No preview yet.\nClick the camera button above to render the current frame.")
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
            _log.debug("preview player: stop failed", exc_info=True)

    def _loop_if_ended(self, status) -> None:
        try:
            if status == QMediaPlayer.MediaStatus.EndOfMedia:
                self.player.setPosition(0)
                self.player.play()
        except Exception:
            _log.debug("preview player: loop-on-end failed", exc_info=True)

    def _toggle_play(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
            self.play_btn.setText("Play")
        else:
            self.player.play()
            self.play_btn.setText("Pause")

    def _toggle_mute(self) -> None:
        muted = not self.audio.isMuted()
        self.audio.setMuted(muted)
        self.mute_btn.setText("Unmute" if muted else "Mute")


