"""Typing-only base for the extracted window mixins.

The 5,000-line ``BlenderVideoMapperQt`` is being carved into cohesive mixins
(queue, deadline, runtime, …). Each mixin's methods operate on ``self`` (the
concrete window), so under ``check_untyped_defs`` mypy needs to know the shared
state and cross-called methods exist. This class declares them — all under
``TYPE_CHECKING``, so it has ZERO runtime members; the real values live on the
window. Mixins inherit it purely for typing.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from PySide6.QtCore import QThread, SignalInstance
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import QDialog, QDockWidget, QLabel

    # For typing, mixins ARE a QWidget (the concrete window is a QMainWindow), so
    # `QMessageBox(self, …)` type-checks. At runtime the base is plain ``object``
    # — no second QWidget in the MRO — and the real window supplies QMainWindow.
    # Imported under the alias (not assigned) so every mypy version accepts it
    # as a base class.
    from PySide6.QtWidgets import QWidget as _Base

    from core.asset_grouping import GroupingConfig
    from core.models import RenderJob
    from panels import (
        DeadlinePanel,
        PresetBrowserPanel,
        QueuePanel,
        RenderPanel,
        ScenePanel,
        WatchPanel,
    )
    from theme import Palette
    from workers import DeadlineQueryThread, FuncThread
else:
    _Base = object


class _WindowMembers(_Base):
    if TYPE_CHECKING:
        # ── Shared state set in BlenderVideoMapperQt.__init__ ──────────────
        _jobs: list[RenderJob]
        _active_job_id: int | None
        _next_job_id: int
        _is_rendering: bool
        _in_select_guard: bool
        _loading_job_into_ui: bool
        _deadline_repo_path: str
        _deadline_command_path: str
        _deadline_comment: str
        _deadline_job_name_template: str
        _deadline_test_thread: DeadlineQueryThread | None
        _farm_nodes_thread: DeadlineQueryThread | None
        _blender_path: str
        _palette: Palette
        # ── Updates (UpdateMixin) ──────────────────────────────────────────
        _update_checked: SignalInstance       # Signal lives on the window (QObject)
        _update_check_thread: FuncThread | None
        _shutting_down: bool
        _check_updates_on_launch: bool
        _skipped_update: str
        _sb_update: QLabel
        # ── Managed runtime install (RuntimeMixin) ─────────────────────────
        _runtime_prompted: bool
        _runtime_install_thread: QThread | None
        _runtime_progress_dialog: QDialog | None
        # ── Post-run reporting (ReportMixin) ───────────────────────────────
        _job_durations: dict[int, float]
        _job_metrics: dict[int, dict]
        _power_watts: float
        _power_rate: float
        _last_report_path: str
        _last_html_report_path: str
        _open_report_action: QAction
        _open_html_action: QAction
        # ── Watch & auto-render (WatchMixin) ───────────────────────────────
        _asset_grouping: GroupingConfig
        _asset_group_jobs: dict[tuple[int, int], tuple[int, int]]
        _pending_autorender_ids: set[int]
        _autorender_enabled: bool
        _autorender_pattern: str
        _autorender_output: str
        _autorender_start: bool
        _deliver_dir: str
        _watch_first_run_seen: bool
        _discovered_materials: list[str]
        watch_panel: WatchPanel
        watch_dock: QDockWidget
        scene_panel: ScenePanel
        render_panel: RenderPanel
        deadline_panel: DeadlinePanel
        queue_panel: QueuePanel
        presets_panel: PresetBrowserPanel

        # ── Methods provided by the window / other mixins ──────────────────
        def _append_log(self, line: str) -> None: ...
        def _is_headless(self) -> bool: ...                          # window
        def _on_queue_job_selected(self, job_id: int) -> None: ...   # QueueMixin
        def _refresh_job_outputs(self) -> None: ...                  # QueueMixin
        def _refresh_queue_view(self) -> None: ...                   # QueueMixin
        def _job_etas(self) -> dict[int, str]: ...                   # window
        def _show_toast(self, message: str, kind: str = ...) -> None: ...
        def _show_properties_dialog(self, initial_tab: str | None = ...) -> None: ...
        def _schedule_save(self) -> None: ...
        def _save_profile(self) -> None: ...
        def _push_undo(self, desc: str, restore) -> None: ...
        def _update_status_bar(self) -> None: ...
        def _update_progress_caption(self) -> None: ...
        def _make_job_snapshot(self, job: RenderJob, assignments: list) -> None: ...
        def _start_render(self, render_all: bool = ..., only_job_ids: set | None = ...) -> None: ...
        def _unsaved_floating_changes(self) -> bool: ...
        def _effective_chunk_size(self, job: RenderJob) -> int: ...
        def _fmt_dur(self, seconds: float) -> str: ...               # window (ReportMixin uses)
        @staticmethod
        def _sheet_path_for(output: str) -> Path | None: ...         # window (ReportMixin uses)
        @staticmethod
        def _open_path(path: str) -> None: ...                       # window (ReportMixin uses)
        @staticmethod
        def _friendly_error_hint(text: str) -> str: ...
