"""App-styled message dialogs.

A modern, on-brand replacement for the native ``QMessageBox`` question / info /
warning / critical boxes (which render with a generic platform icon and default
chrome). ``MessageDialog`` is a frameless, rounded, modal card that follows the
app's palette + button language; the module-level helpers (``confirm``,
``inform``, ``warn``, ``error``, ``ask``) are near drop-in replacements for the
``QMessageBox`` static methods.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

import icons
from theme import active_palette, mix

# kind → (icon glyph, palette attribute used to tint it)
_KIND = {
    "question": ("help", "accent"),
    "info": ("info", "info"),
    "warning": ("alert", "warning"),
    "danger": ("alert", "danger"),
    "success": ("check", "success"),
}


class MessageDialog(QDialog):
    """A frameless, app-styled modal. ``value`` holds the clicked button's value
    (or ``None`` if dismissed with Esc)."""

    def __init__(self, parent, title, message="", *, kind="question",
                 buttons=None, default=None):
        super().__init__(parent)
        self.setObjectName("MessageDialog")
        self._value = None
        self._drag_offset = None
        pal = active_palette()

        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 18)   # breathing room for the shadow

        card = QFrame(self)
        card.setObjectName("DialogCard")
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 150))
        card.setGraphicsEffect(shadow)
        outer.addWidget(card)

        lay = QVBoxLayout(card)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(16)

        # Header: tinted icon chip + title/body column.
        top = QHBoxLayout()
        top.setSpacing(14)
        glyph_name, tint_attr = _KIND.get(kind, _KIND["question"])
        tint = getattr(pal, tint_attr)
        chip = QLabel()
        chip.setPixmap(icons.pixmap(glyph_name, tint, 24))
        chip.setFixedSize(40, 40)
        chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chip.setStyleSheet(f"background:{mix(pal.surface, tint, 0.18)}; border-radius:20px;")
        top.addWidget(chip, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(6)
        title_lbl = QLabel(title)
        title_lbl.setObjectName("DialogTitle")
        title_lbl.setWordWrap(True)
        col.addWidget(title_lbl)
        if message:
            body = QLabel(message)
            body.setObjectName("DialogBody")
            body.setWordWrap(True)
            col.addWidget(body)
        col.addStretch(1)
        top.addLayout(col, 1)
        lay.addLayout(top)

        # Buttons, right-aligned; affirmative carries the accent/danger styling.
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addStretch(1)
        for label, value, role in (buttons or [("OK", True, "primary")]):
            btn = QPushButton(label)
            if role == "primary":
                btn.setObjectName("PrimaryButton")
            elif role == "danger":
                btn.setObjectName("DangerButton")
            btn.setMinimumWidth(84)
            btn.clicked.connect(lambda _=False, v=value: self._choose(v))
            is_default = (value == default) if default is not None else (role in ("primary", "danger"))
            btn.setDefault(is_default)
            btn.setAutoDefault(is_default)
            row.addWidget(btn)
        lay.addLayout(row)

        self.setMinimumWidth(380)
        self.setMaximumWidth(520)

    # ── behaviour ────────────────────────────────────────────────────────
    def _choose(self, value):
        self._value = value
        self.accept()

    @property
    def value(self):
        return self._value

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._value = None
            self.reject()
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        # Centre over the parent window (frameless dialogs otherwise land at 0,0).
        par = self.parentWidget()
        if par is not None:
            center = par.window().frameGeometry().center()
            self.move(center - self.rect().center())

    # Drag the frameless card around by pressing anywhere on it.
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, event):
        self._drag_offset = None


# ── drop-in helpers ──────────────────────────────────────────────────────────
def confirm(parent, title, message="", *, ok="Yes", cancel="No", danger=False) -> bool:
    """A yes/no confirmation. Returns True if the affirmative was chosen.
    Set ``danger=True`` for destructive actions (red affirmative button)."""
    dlg = MessageDialog(
        parent, title, message,
        kind="danger" if danger else "question",
        buttons=[(cancel, False, "neutral"), (ok, True, "danger" if danger else "primary")],
        default=True)
    dlg.exec()
    return bool(dlg.value)


def inform(parent, title, message="", *, kind="info", ok="OK") -> None:
    """A single-button notice (replaces QMessageBox.information)."""
    MessageDialog(parent, title, message, kind=kind,
                  buttons=[(ok, True, "primary")], default=True).exec()


def warn(parent, title, message="") -> None:
    """A single-button warning (replaces QMessageBox.warning)."""
    inform(parent, title, message, kind="warning")


def error(parent, title, message="") -> None:
    """A single-button error (replaces QMessageBox.critical)."""
    inform(parent, title, message, kind="danger")


def ask(parent, title, message="", *, buttons, kind="question", default=None):
    """A multi-choice dialog. ``buttons`` is a list of ``(label, value, role)``
    where role is "primary", "danger" or "neutral". Returns the chosen value,
    or None if dismissed with Esc."""
    dlg = MessageDialog(parent, title, message, kind=kind, buttons=buttons, default=default)
    dlg.exec()
    return dlg.value
