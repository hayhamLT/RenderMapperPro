"""Render-settings presets (.rmpreset) — save / load / browse reusable recipes
and apply them to the queue. Extracted verbatim from BlenderVideoMapperQt."""
from __future__ import annotations

import dataclasses
import json
import re
from pathlib import Path

from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from app_window.base import _WindowMembers
from core.logging_setup import get_logger
from core.models import RenderOptions

_log = get_logger(__name__)

# Preset storage (moved here — only the preset subsystem uses these).
PRESETS_DIR = Path.home() / ".blender_video_mapper" / "presets"
PRESET_EXT = ".rmpreset"     # reusable render-settings recipe


class PresetMixin(_WindowMembers):

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
            _log.debug("could not ensure presets directory exists", exc_info=True)
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

    def _refresh_preset_browser(self) -> None:
        try:
            PRESETS_DIR.mkdir(parents=True, exist_ok=True)
            presets = sorted(PRESETS_DIR.glob(f"*{PRESET_EXT}"), key=lambda x: x.stem.lower())
        except Exception:
            presets = []
        self.presets_panel.set_presets(presets)
