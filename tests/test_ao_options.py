"""Ambient-occlusion option plumbing: render_options + preset round-trip."""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _panel():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from panels import RenderPanel
    return RenderPanel()


def test_render_options_carry_ao():
    p = _panel()
    p.ao_cb.setChecked(True)
    p.ao_distance_edit.setText("0.35")
    p.ao_factor_edit.setText("1.5")
    o = p.render_options()
    assert o.ao_enabled is True
    assert o.ao_distance == 0.35
    assert o.ao_factor == 1.5


def test_ao_defaults_off():
    o = _panel().render_options()
    assert o.ao_enabled is False


def test_ao_survives_preset_round_trip():
    p = _panel()
    p.ao_cb.setChecked(True)
    p.ao_distance_edit.setText("0.4")
    p.ao_factor_edit.setText("2.0")
    d = p.settings_dict()
    p2 = _panel()
    p2.apply_settings(d)
    o = p2.render_options()
    assert o.ao_enabled is True
    assert o.ao_distance == 0.4
    assert o.ao_factor == 2.0


def test_ao_box_blender_only():
    # isHidden() reflects the widget's own setVisible flag (set by set_renderer),
    # independent of the collapsed-by-default Advanced section.
    p = _panel()
    p.set_renderer("CYCLES")       # Blender Cycles
    assert p.ao_box.isHidden() is False
    p.set_renderer("BLENDER_EEVEE")  # Blender EEVEE — AO still applies
    assert p.ao_box.isHidden() is False
    p.set_renderer("Redshift")     # C4D/Redshift
    assert p.ao_box.isHidden() is True
    p.set_renderer("WEB_THREEJS")  # web/three.js
    assert p.ao_box.isHidden() is True
