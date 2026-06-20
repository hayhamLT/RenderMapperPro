"""Post-run reporting — the shareable JSON + HTML render report (per-job status,
timing, sec/frame, cost, embedded contact sheets) and the actions that open
them. Extracted verbatim from BlenderVideoMapperQt (operates on ``self``). Render
*history* (HISTORY_PATH, monkeypatched by tests) stays in app_qt; this mixin only
reads live job state via ``self``."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app_window.base import _WindowMembers
from core.logging_setup import get_logger
from core.metrics import estimate_energy_cost
from core.utils import atomic_write_text

_log = get_logger(__name__)

REPORTS_DIR = Path.home() / ".blender_video_mapper" / "reports"


class ReportMixin(_WindowMembers):

    def _write_run_report(self) -> None:
        try:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            report = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "blender": self._blender_path,
                "scene": self.scene_panel.scene_edit.text().strip(),
                "jobs": [
                    {
                        "label": j.label,
                        "status": j.status,
                        "output": j.output_path,
                        "frames": (f"{j.render_options.frame_start}-{j.render_options.frame_end}"
                                   if j.render_options else ""),
                        "duration_s": round(self._job_durations.get(j.id, 0.0), 1),
                        "error": j.error,
                    }
                    for j in self._jobs
                ],
            }
            path = REPORTS_DIR / f"run_report_{stamp}.json"
            atomic_write_text(path, json.dumps(report, indent=2))
            self._last_report_path = str(path)
            if hasattr(self, "_open_report_action"):
                self._open_report_action.setEnabled(True)
            try:
                html_path = REPORTS_DIR / f"run_report_{stamp}.html"
                atomic_write_text(html_path, self._build_html_report())
                self._last_html_report_path = str(html_path)
                if hasattr(self, "_open_html_action"):
                    self._open_html_action.setEnabled(True)
            except Exception:
                _log.warning("failed to write the HTML render report", exc_info=True)
        except Exception:
            _log.warning("failed to write the run report", exc_info=True)

    def _build_html_report(self) -> str:
        """A standalone, shareable HTML report of the last run: per-job status,
        timing, sec/frame, error text, and embedded contact-sheet thumbnails."""
        import html as _html
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        scene = _html.escape(self.scene_panel.scene_edit.text().strip() or "—")
        rows = []
        for j in self._jobs:
            opts = j.render_options
            frames = f"{opts.frame_start}-{opts.frame_end}" if opts else ""
            dur = self._fmt_dur(self._job_durations.get(j.id, 0.0))
            m = self._job_metrics.get(j.id, {})
            spf = f"{m['avg_spf']:.1f}s" if m.get("avg_spf") else "—"
            _kwh, _cost = estimate_energy_cost(
                self._job_durations.get(j.id, 0.0), self._power_watts, self._power_rate)
            cost = f"${_cost:.2f}" if _cost else "—"
            color = {"success": "#3ba55d", "failed": "#ed4245",
                     "cancelled": "#888"}.get(j.status, "#bbb")
            sheet_html = ""
            sheet = self._sheet_path_for(j.output_path) if j.status == "success" else None
            if sheet is not None:
                try:
                    sheet_html = f'<div><img src="{_html.escape(sheet.as_uri())}" loading="lazy"></div>'
                except Exception:
                    sheet_html = ""
            err = f'<div class="err">{_html.escape(j.error)}</div>' if j.error else ""
            extra = f'<tr><td colspan="6">{sheet_html}{err}</td></tr>' if (sheet_html or err) else ""
            rows.append(
                f'<tr><td>{_html.escape(j.label or f"Job {j.id}")}</td>'
                f'<td style="color:{color};font-weight:600">{_html.escape(j.status)}</td>'
                f'<td>{_html.escape(frames)}</td><td>{_html.escape(dur)}</td>'
                f'<td>{_html.escape(spf)}</td><td>{_html.escape(cost)}</td></tr>{extra}')
        body = "\n".join(rows)
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            f'<title>Render Report — {stamp}</title><style>'
            'body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#15171c;'
            'color:#e6e6e6;margin:0;padding:32px}'
            'h1{font-size:20px;margin:0 0 4px}.sub{color:#9aa0a6;font-size:13px;margin-bottom:24px}'
            'table{border-collapse:collapse;width:100%}'
            'th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #2a2d34;'
            'font-size:13px;vertical-align:top}'
            'th{color:#9aa0a6;font-weight:600;text-transform:uppercase;font-size:11px;letter-spacing:.04em}'
            'img{max-width:100%;border-radius:6px;margin:8px 0}'
            '.err{color:#ed4245;font-family:ui-monospace,monospace;font-size:12px;'
            'white-space:pre-wrap;margin:6px 0}.brand{color:#e8833a;font-weight:700}'
            '</style></head><body>'
            '<h1><span class="brand">Render Mapper Pro</span> — Render Report</h1>'
            f'<div class="sub">{stamp} · Scene: {scene}</div>'
            '<table><thead><tr><th>Job</th><th>Status</th><th>Frames</th>'
            '<th>Duration</th><th>Avg/frame</th><th>Est. Cost</th></tr></thead>'
            f'<tbody>{body}</tbody></table></body></html>')

    def _open_last_report(self) -> None:
        if self._last_report_path and Path(self._last_report_path).exists():
            self._open_path(self._last_report_path)
        else:
            self._show_toast("No run report yet", "warning")

    def _open_html_report(self) -> None:
        if self._last_html_report_path and Path(self._last_html_report_path).exists():
            self._open_path(self._last_html_report_path)
        else:
            self._show_toast("No HTML report yet — run a render first.", "warning")
