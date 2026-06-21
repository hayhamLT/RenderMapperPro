"""Queue management — the render-job list: build/sync from the scene, queue,
reorder, remove, requeue, duplicate, set priority, and the per-job output/error
actions. Extracted verbatim from BlenderVideoMapperQt (operates on ``self``)."""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import QInputDialog, QMessageBox

from app_window.base import _WindowMembers
from core.models import MaterialVideoAssignment, RenderJob
from core.utils import OUTPUT_PROFILES, resolve_output_path
from media import reveal_in_file_manager


class QueueMixin(_WindowMembers):

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
        job.extra_output_profiles = self.render_panel.extra_output_profiles()
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
        self.queue_panel.set_jobs(self._jobs, self._job_etas())
        if self._active_job_id is not None:
            self.queue_panel.select_job(self._active_job_id)
        self._update_status_bar()

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
            self.render_panel.set_extra_output_profiles(getattr(job, "extra_output_profiles", []))

            self.render_panel.output_edit.setText(job.output_input or "")
        finally:
            self._loading_job_into_ui = False

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
            return None
        return p

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

    @staticmethod
    def _job_has_mapping(job: RenderJob) -> bool:
        """A job is renderable only if a video is connected to a material."""
        return any(a.material_name and a.video_path for a in (job.material_assignments or []))
