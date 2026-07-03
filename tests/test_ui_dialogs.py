"""MessageDialog behaviour: value semantics, Esc, keyboard navigation, focus,
and the accessibility surface (window title, accessible names). Offscreen Qt;
no exec() — everything is driven through the widget API and key events."""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from ui_dialogs import MessageDialog

app = QApplication.instance() or QApplication([])


def _key(dlg, key):
    dlg.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, key,
                                Qt.KeyboardModifier.NoModifier))


@pytest.fixture
def three_btn():
    dlg = MessageDialog(
        None, "Remove clips?", "This can't be undone.",
        kind="danger",
        buttons=[("Cancel", "cancel", "neutral"),
                 ("Keep", "keep", "neutral"),
                 ("Remove", "remove", "danger")],
        default="cancel")
    yield dlg
    dlg.deleteLater()


def test_choose_sets_value_and_accepts(three_btn):
    three_btn._choose("remove")
    assert three_btn.value == "remove"


def test_escape_means_dismissed(three_btn):
    _key(three_btn, Qt.Key.Key_Escape)
    assert three_btn.value is None


def test_accessibility_surface(three_btn):
    assert three_btn.windowTitle() == "Remove clips?"
    assert three_btn.accessibleName() == "Remove clips?"
    assert three_btn.accessibleDescription() == "This can't be undone."
    assert [b.accessibleName() for b in three_btn._buttons] == ["Cancel", "Keep", "Remove"]


def test_initial_focus_is_the_default_button(three_btn):
    three_btn.show()
    app.processEvents()
    assert three_btn.focusWidget() is three_btn._buttons[0]   # "Cancel" = default
    three_btn.hide()


def test_arrow_keys_cycle_buttons(three_btn):
    three_btn.show()
    app.processEvents()
    _key(three_btn, Qt.Key.Key_Right)
    assert three_btn.focusWidget().text() == "Keep"
    _key(three_btn, Qt.Key.Key_Right)
    assert three_btn.focusWidget().text() == "Remove"
    _key(three_btn, Qt.Key.Key_Right)                          # wraps
    assert three_btn.focusWidget().text() == "Cancel"
    _key(three_btn, Qt.Key.Key_Left)                           # and back
    assert three_btn.focusWidget().text() == "Remove"
    three_btn.hide()


def test_return_activates_focused_not_hidden_default(three_btn):
    # All buttons are autoDefault: with focus on "Keep", Return must never fire
    # the danger default under the user's fingers.
    three_btn.show()
    app.processEvents()
    _key(three_btn, Qt.Key.Key_Right)                          # focus "Keep"
    focused = three_btn.focusWidget()
    assert focused.text() == "Keep" and focused.autoDefault()
    focused.click()                                            # what Return triggers
    assert three_btn.value == "keep"


def test_default_falls_back_to_role_when_not_given():
    dlg = MessageDialog(None, "T", buttons=[("No", False, "neutral"),
                                            ("Yes", True, "primary")])
    assert dlg._default_btn is not None and dlg._default_btn.text() == "Yes"
    dlg.deleteLater()
