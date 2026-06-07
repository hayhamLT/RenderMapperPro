import re

with open("app_qt.py", "r") as f:
    c = f.read()

# 1. Import
c = c.replace(
    "from core.runner import run_blender_job",
    "from core.runner import run_blender_job, submit_deadline_job"
)

# 2. RenderJob model
c = c.replace(
    "safe_mode: bool = True\n    status: str = &idle&",
    "safe_mode: bool = True\n    use_deadline: bool = False\n    deadline_pool: str = &&\n    deadline_priority: int = 50\n    status: str = &idle&".replace("&", "\"")
)

# 3. RenderPainel UI
ui_insert = """
        # Deadline Integration
        root.addWidget(section("DEADLINE RENDER FARM"))
        dl_row = QHBoxLayout()
        dl_row.setSpacing(6)
        self.use_dl_cb = QCheckBox("Submit to Deadline")
        self.dl_pool_edit = QLineEdit()
        self.dl_pool_edit.setPlaceholderText("Pool (optional)")
        self.dl_prio_edit = QLineEdit("50")
        self.dl_prio_edit.setPlaceholderText("Priority")
        dl_row.addWidget(self.use_dl_cb)
        dl_row.addWidget(self.dl_pool_edit)
        dl_row.addWidget(QLabel("Priority:"))
        dl_row.addWidget(self.dl_prio_edit)
        root.addLayout(dl_row)
"""
c = c.replace(
    "root.addStretch()",
    ui_insert + "\n        root.addStretch()"
) or c

# 4. _make_job_snapshot
snap_insert = """
        job.use_deadline = self.render_panel.use_dl_cb.isChecked()
        job.deadline_pool = self.render_panel.dl_pool_edit.text().strip()
        try:
            job.deadline_priority = int(self.render_panel.dl_prio_edit.text().strip() or "50")
        except ValueError:
            job.deadline_priority = 50
"""
c = c.replace(
    "job.safe_mode = True",
    "job.safe_mode = True\n" + snap_insert
) or c

# 5. _start_render
job_cfg_old = """
            target_camera=j.target_camera,
            output_path=j.output_path,
            render=opts,
            safe_mode=j.safe_mode,
            material_assignments=asn,
        )
"""
job_cfg_new = """
            target_camera=j.target_camera,
            output_path=j.output_path,
            render=opts,
            safe_mode=j.safe_mode,
            use_deadline=getattr(j, 'use_deadline', False),
            deadline_pool=getattr(j, 'deadline_pool', ""),
            deadline_priority=getattr(j, 'deadline_priority', 50),
            material_assignments=asn,
        )
"""
c = c.replace(job_cfg_old.strip(), job_cfg_new.strip()) or c

# 6. RenderThread.run
run_blender_old = """
            try:
                rc = run_blender_job(
                    blender_executable=self.blender,
                    worker_script_path=self.worker,
                    job=cfg,
                    on_log=on_log,
                    should_cancel=lambda: self._skip_current,
                )
"""
run_blender_new = """
            try:
                if getattr(cfg, "use_deadline", False):
                    rc = submit_deadline_job(
                        blender_executable=self.blender,
                        worker_script_path=self.worker,
                        job=cfg,
                        on_log=on_log,
                    )
                else:
                    rc = run_blender_job(
                        blender_executable=self.blender,
                        worker_script_path=self.worker,
                        job=cfg,
                        on_log=on_log,
                        should_cancel=lambda: self._skip_current,
                    )
"""
a = c
c = c.replace(run_blender_old.strip(), run_blender_new.strip())

# 7. Signals for settings changed
signals_insert = """
        self.render_panel.use_dl_cb.toggled.connect(lambda _v: self._on_settings_changed())
        self.render_panel.dl_pool_edit.textChanged.connect(lambda _v: self._on_settings_changed())
        self.render_panel.dl_prio_edit.textChanged.connect(lambda _v: self._on_settings_changed())
"""
a = c
c = c.replace(
    "self.render_panel.engine_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())",
    "self.render_panel.engine_combo.currentTextChanged.connect(lambda _v: self._on_settings_changed())\n" + signals_insert
)

# loading Job into ui
load_ui_old = """
            self.render_panel.width_edit.setText(str(opts.width))
"""
load_ui_new = """
            self.render_panel.width_edit.setText(str(opts.width))
            self.render_panel.use_dl_cb.setChecked(getattr(job, 'use_deadline', False))
            self.render_panel.dl_pool_edit.setText(getattr(job, 'deadline_pool', ""))
            self.render_panel.dl_prio_edit.setText(str(getattr(job, 'deadline_priority', 50)))
"""
a = c
c = c.replace(load_ui_old.strip(), load_ui_new.strip())

with open(b"app_qt.py", "w") as f:
    f.write(c)
