"""Live-log progress coalescing: percentage / fraction ticks collapse onto one
in-place updating bar, while key=value numbers (scale=50%, frame=12) are not
mistaken for progress."""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _panel():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from panels import LogsPanel
    return LogsPanel()


def test_parse_progress_detects_percent_and_fraction():
    from panels import LogsPanel
    p = LogsPanel._parse_progress
    assert p("[12:00:01] [runtime] Download 40% — 200/512 MB")[0] == 40
    assert p("[web] frame 12/24")[0] == 50
    # The key strips numbers + timestamp so successive ticks share one id.
    assert p("[10:00:00] [runtime] Download 10%")[3] == p("[10:00:09] [runtime] Download 90%")[3]


def test_parse_progress_ignores_key_value_numbers():
    from panels import LogsPanel
    p = LogsPanel._parse_progress
    assert p("[app] Preview: frame=12 scale=50% engine=Blender") is None
    assert p("[app] just a normal message with no progress") is None


def test_progress_ticks_coalesce_to_one_row():
    panel = _panel()
    panel.append("[12:00:00] [app] start")
    for pct in (10, 45, 80, 100):
        panel.append(f"[12:00:00] [runtime] Download {pct}% — {pct}/100 MB")
    panel.append("[12:00:09] [app] done")
    # start + ONE coalesced bar + done = 3 rows (not 6).
    assert len(panel._raw) == 3, panel._raw
    assert "█" in panel._raw[1] and "100%" in panel._raw[1]
    assert panel._raw[0].endswith("start") and panel._raw[2].endswith("done")


def test_new_task_starts_a_fresh_bar():
    panel = _panel()
    panel.append("[12:00:00] [web] Chromium download 20%")
    panel.append("[12:00:01] [web] Chromium download 90%")   # same task → coalesce
    panel.append("[12:00:02] [web] frame 5/10")              # different task → new bar
    assert len(panel._raw) == 2
