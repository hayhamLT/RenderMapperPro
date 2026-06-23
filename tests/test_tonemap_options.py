"""Redshift tone-map / exposure option plumbing: render_options, scene-settings
apply, profile round-trip, and Redshift-only visibility."""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _panel():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from panels import RenderPanel
    return RenderPanel()


def test_tonemap_defaults_to_filmic():
    o = _panel().render_options()
    assert o.rs_tonemap == "filmic"
    assert o.rs_exposure == 0.0


def test_render_options_carry_tonemap():
    p = _panel()
    p.rs_tonemap_combo.setCurrentText("Reinhard")
    p.rs_exposure_edit.setText("-2.5")
    o = p.render_options()
    assert o.rs_tonemap == "reinhard"
    assert o.rs_exposure == -2.5


def test_linear_choice_maps_to_linear():
    p = _panel()
    p.rs_tonemap_combo.setCurrentText("Linear (raw)")
    assert p.render_options().rs_tonemap == "linear"


def test_apply_scene_settings_brings_tonemap_and_exposure():
    # What c4d_discover reports for an ACES-view scene with an exposure offset.
    p = _panel()
    p.apply_scene_settings({"rs_tonemap": "filmic", "rs_exposure": -1.5})
    assert p.render_options().rs_tonemap == "filmic"
    assert p.render_options().rs_exposure == -1.5
    p.apply_scene_settings({"rs_tonemap": "linear"})
    assert p.render_options().rs_tonemap == "linear"


def test_tonemap_survives_profile_round_trip():
    p = _panel()
    p.rs_tonemap_combo.setCurrentText("Reinhard")
    p.rs_exposure_edit.setText("-3.0")
    d = p.settings_dict()
    p2 = _panel()
    p2.apply_settings(d)
    o = p2.render_options()
    assert o.rs_tonemap == "reinhard"
    assert o.rs_exposure == -3.0


def test_tonemap_box_redshift_only():
    # isHidden() reflects the widget's own setVisible flag (set by set_renderer).
    p = _panel()
    p.set_renderer("Redshift")
    assert p.rs_color_box.isHidden() is False
    p.set_renderer("CYCLES")
    assert p.rs_color_box.isHidden() is True
    p.set_renderer("BLENDER_EEVEE")
    assert p.rs_color_box.isHidden() is True
    p.set_renderer("WEB_THREEJS")
    assert p.rs_color_box.isHidden() is True
