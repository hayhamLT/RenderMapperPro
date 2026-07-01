"""Watch folder & auto-render — the ingest pipeline: panel load/apply wiring,
auto-map and previz (asset-grouping) job builders, and the dry-run assembly
preview. Extracted verbatim from BlenderVideoMapperQt (operates on ``self``)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from app_window.base import _WindowMembers
from core.asset_grouping import GroupingConfig as AssetGroupingConfig
from core.asset_grouping import group_clips, parse_clip
from core.models import VIDEO_MAPPING_MODE_EMISSION, MaterialVideoAssignment, RenderJob
from core.utils import (
    IMAGE_MEDIA_EXTENSIONS,
    OUTPUT_PROFILES,
    VIDEO_EXTENSIONS,
    ext_for_format,
)
from ui_dialogs import inform


class WatchMixin(_WindowMembers):

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
        if hasattr(self, "watch_panel"):
            self.watch_panel.set_mode(self._asset_grouping.enabled)

    def _refresh_watch_panel(self, *_a) -> None:
        """Mirror the watch engine's folder/enabled/mode into the Watch panel."""
        if not hasattr(self, "watch_panel"):
            return
        folder, enabled = self.scene_panel.get_watch_folder()
        self.watch_panel.set_folder_state(folder, enabled)
        self.watch_panel.set_mode(self._asset_grouping.enabled)

    def _load_watch_panel(self) -> None:
        """Populate the Watch panel from the current config (folder + grouping +
        auto-render + file-stability). The panel suppresses signals while loading."""
        if not hasattr(self, "watch_panel"):
            return
        ag = self._asset_grouping
        folder, enabled = self.scene_panel.get_watch_folder()
        interval_ms, settle = self.scene_panel.get_watch_options()
        self.watch_panel.set_folder_state(folder, enabled)
        self.watch_panel.load_config(
            grouping_enabled=ag.enabled,
            pattern=ag.pattern,
            content_type=ag.content_type,
            output_template=ag.output_template,
            autorender_pattern=self._autorender_pattern,
            screen_to_material=dict(ag.screen_to_material),
            setup_to_scene=dict(ag.setup_to_scene),
            settle_s=settle,
            poll_interval_s=interval_ms / 1000.0,
            autorender_start=self._autorender_start,
            output_dir=self._autorender_output,
            deliver_dir=self._deliver_dir,
        )
        # First-run banner: only until acknowledged, and only before any folder is set.
        seen = getattr(self, "_watch_first_run_seen", False)
        self.watch_panel.show_first_run(not seen and not folder)

    def _apply_watch_panel(self) -> None:
        """Write the Watch panel's settings back onto the live config + engine and
        persist. Connected to the panel's ``config_changed`` (fires on any edit)."""
        if not hasattr(self, "watch_panel"):
            return
        c = self.watch_panel.get_config()
        ag = self._asset_grouping
        ag.enabled = bool(c["grouping_enabled"])
        ag.pattern = c["pattern"] or ag.pattern
        ag.content_type = c["content_type"]
        ag.output_template = c["output_template"] or ag.output_template
        ag.screen_to_material = dict(c["screen_to_material"])
        ag.setup_to_scene = dict(c["setup_to_scene"])
        self._autorender_pattern = c["autorender_pattern"] or self._autorender_pattern
        self._autorender_output = c["output_dir"]
        self._autorender_start = bool(c["autorender_start"])
        self._deliver_dir = c["deliver_dir"]
        # In auto-map mode the checkbox also gates whether dropped clips render at
        # all; in previz mode jobs are always built, so it only gates auto-start.
        if not ag.enabled:
            self._autorender_enabled = bool(c["autorender_start"])
        self.scene_panel.set_watch_options(int(max(1.0, float(c["poll_interval_s"])) * 1000),
                                           float(c["settle_s"]))
        self.scene_panel.set_grouping_mode(ag.enabled)
        self._save_profile()

    def _pick_watch_output_dir(self) -> None:
        cur = self._autorender_output or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose render output folder", cur)
        if d:
            self.watch_panel.out_dir_edit.setText(d)   # triggers config_changed → apply

    def _pick_watch_deliver_dir(self) -> None:
        cur = self._deliver_dir or str(Path.home())
        d = QFileDialog.getExistingDirectory(self, "Choose delivery folder", cur)
        if d:
            self.watch_panel.deliver_edit.setText(d)   # triggers config_changed → apply

    def _preview_watch_assembly(self) -> None:
        """Dry-run the previz grouping from the panel's current config."""
        self._apply_watch_panel()        # make sure _asset_grouping reflects the panel
        self._preview_assembly(self._asset_grouping)

    def _open_watch_render_panel(self) -> None:
        """Reveal + focus the Watch & Auto-render dock (the editable home)."""
        self.watch_dock.show()
        self.watch_dock.raise_()
        self.watch_panel.setFocus()

    def _on_watch_first_run_dismissed(self) -> None:
        self._watch_first_run_seen = True
        self._save_profile()

    def _pick_watch_folder(self) -> None:
        folder, _en = self.scene_panel.get_watch_folder()
        d = QFileDialog.getExistingDirectory(self, "Choose watch folder", folder or str(Path.home()))
        if d:
            self.scene_panel.set_watch_folder(d, True)
            self._refresh_watch_panel()
            self.watch_panel.add_activity("Watch", f"Watching {d}")

    def _toggle_watch_from_panel(self, on: bool) -> None:
        folder, _en = self.scene_panel.get_watch_folder()
        if on and not folder:
            self._pick_watch_folder()   # nothing to watch yet — pick one first
            return
        self.scene_panel.set_watch_folder(folder, on)
        self._refresh_watch_panel()
        self.watch_panel.add_activity("Watch", "Started" if on else "Stopped")

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
        self.watch_panel.add_activity(
            "Previz", f"{created} new + {updated} updated job(s) from {len(groups)} group(s)")
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
            inform(self, "Preview Assembly",
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
