"""Shared pytest configuration.

Force the headless Qt platform so UI-touching tests run without a display and
never pop real windows during a local ``pytest`` run (CI already sets this).
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
