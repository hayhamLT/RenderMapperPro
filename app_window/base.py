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
    from PySide6.QtWidgets import QWidget

    from core.models import RenderJob
    from panels import DeadlinePanel, PresetBrowserPanel, QueuePanel, RenderPanel, ScenePanel

    # For typing, mixins ARE a QWidget (the concrete window is a QMainWindow), so
    # `QMessageBox(self, …)` type-checks. At runtime the base is plain ``object``
    # — no second QWidget in the MRO — and the real window supplies QMainWindow.
    _Base = QWidget
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
        scene_panel: ScenePanel
        render_panel: RenderPanel
        deadline_panel: DeadlinePanel
        queue_panel: QueuePanel
        presets_panel: PresetBrowserPanel

        # ── Methods provided by the window / other mixins ──────────────────
        def _append_log(self, line: str) -> None: ...
        def _on_queue_job_selected(self, job_id: int) -> None: ...   # QueueMixin
        def _refresh_job_outputs(self) -> None: ...                  # QueueMixin
        def _refresh_queue_view(self) -> None: ...                   # QueueMixin
        def _show_toast(self, message: str, kind: str = ...) -> None: ...
        def _schedule_save(self) -> None: ...
        def _push_undo(self, desc: str, restore) -> None: ...
        def _update_health(self) -> None: ...
        def _update_status_bar(self) -> None: ...
        def _update_progress_caption(self) -> None: ...
        def _unsaved_floating_changes(self) -> bool: ...
        def _effective_chunk_size(self, job: RenderJob) -> int: ...
        @staticmethod
        def _friendly_error_hint(text: str) -> str: ...
