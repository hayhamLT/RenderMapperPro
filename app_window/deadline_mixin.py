"""Deadline render-farm subsystem — config validation, Test Connection (pools/
groups/machines), the Farm Nodes dialog, submit-warnings, and Export Job Files.
Extracted verbatim from BlenderVideoMapperQt (operates on ``self``)."""
from __future__ import annotations

import dataclasses
import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app_window.base import _WindowMembers
from core.logging_setup import get_logger
from core.models import JobConfig, MaterialVideoAssignment, RenderJob
from core.utils import OUTPUT_PROFILES, find_deadlinecommand
from workers import DeadlineQueryThread

_log = get_logger(__name__)


class DeadlineMixin(_WindowMembers):

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
                _log.debug("farm-nodes dialog closed before results arrived", exc_info=True)

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
        dp.dl_sec_pool_combo.addItems(["", *pools])
        if current_pool:
            dp.dl_pool_combo.setCurrentText(current_pool)
        if current_sec:
            dp.dl_sec_pool_combo.setCurrentText(current_sec)
        if groups:
            current_group = dp.dl_group_combo.currentText()
            dp.dl_group_combo.clear()
            dp.dl_group_combo.addItems(["", *groups])
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
            from app_qt import _resolve_runtime_script  # local: avoid an import cycle
            worker = _resolve_runtime_script("blender_worker.py")
            worker_path = Path(worker).expanduser().resolve()

            with open(job_info_path, "w") as f:
                # Single source of truth — same writer the farm submit uses, so
                # exported files never drift from submitted ones.
                from core.runner import write_commandline_job_info
                write_commandline_job_info(
                    f, cfg, scene_path.name,
                    Path(cfg.video_path).name if cfg.video_path else "")

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
