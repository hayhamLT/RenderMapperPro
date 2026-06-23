"""Offscreen tests for the visual chip builder (ui_widgets.FilenamePatternBuilder).

It's a view over a QLineEdit holding the canonical pattern; every chip edit must
rewrite that text correctly. The modal actions (rename / add-field use
QInputDialog) aren't driven here — the non-modal edits (toggle/move/delete/
separator) cover the round-trip through core.naming.build_pattern.
"""
import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _make(pattern):
    from PySide6.QtWidgets import QApplication, QLineEdit
    QApplication.instance() or QApplication([])
    from ui_widgets import FilenamePatternBuilder
    edit = QLineEdit(pattern)
    return edit, FilenamePatternBuilder(edit)


def test_constructs_and_renders_parts_plus_add_button():
    _edit, b = _make("{Project}_D{Day#}")
    # 2 chips + 1 separator + "+ Field" button (+ a stretch item) -> several widgets
    assert b._row.count() >= 4


def test_toggle_number_rewrites_pattern():
    edit, b = _make("{Project}_D{Day#}")     # parts: Project, "_D", Day#
    b._toggle(0, "is_number")                # Project -> number
    assert edit.text() == "{Project#}_D{Day#}"


def test_toggle_optional_rewrites_pattern():
    edit, b = _make("{Name}_V{Version#}")    # parts: Name, "_V", Version#
    b._toggle(2, "optional")
    assert edit.text() == "{Name}_V{Version#?}"


def test_move_swaps_parts():
    edit, b = _make("{A}_{B}")               # parts: A, "_", B
    b._move(0, 2)                            # swap the two tokens around the "_"
    assert edit.text() == "{B}_{A}"


def test_delete_removes_part():
    edit, b = _make("{A}_{B}")
    b._delete(0)
    assert edit.text() == "_{B}"


def test_separator_edit_rewrites_literal():
    edit, b = _make("{A}_{B}")
    b._set_sep(1, "-")
    assert edit.text() == "{A}-{B}"


def test_typing_in_the_field_rebuilds_chips():
    edit, b = _make("{A}_{B}")
    edit.setText("{Only#}")                  # textChanged -> _rebuild
    assert b._row.count() >= 1
    # still a live view of the new text
    b._toggle(0, "optional")
    assert edit.text() == "{Only#?}"


def test_partial_pattern_does_not_crash():
    _edit, b = _make("{Project}_D{Day")      # unbalanced brace
    assert b._row.count() >= 1               # rendered as literal text, no exception
