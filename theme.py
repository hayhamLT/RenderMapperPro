"""Central design-token system for Render Mapper Pro.

Single source of truth for colors, spacing, radius and fonts. Replaces the
~25 hard-coded hex values that used to be scattered across inline stylesheets.

Usage:
    from theme import build_palette, stylesheet, ACCENTS
    pal = build_palette("dark", ACCENTS["Blue"])
    widget.setStyleSheet(stylesheet(pal))
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Design scales ────────────────────────────────────────────────────────────
SP_XS = 2
SP_SM = 4
SP_MD = 8
SP_LG = 12
SP_XL = 16

RADIUS_SM = 4
RADIUS = 7
RADIUS_LG = 10

FONT_XS = 10
FONT_SM = 11
FONT_BASE = 13
FONT_LG = 15


@dataclass(frozen=True)
class Palette:
    """A complete set of resolved colors for one theme + accent."""

    mode: str            # "dark" | "light"
    window: str          # app/window background
    surface: str         # panels, inputs
    surface_alt: str     # raised rows, headers
    surface_hover: str   # hover state for list rows / neutral buttons
    border: str
    border_strong: str
    text: str
    text_muted: str
    text_faint: str
    accent: str
    accent_hover: str
    accent_text: str     # text/icon color on top of accent
    neutral_btn: str     # neutral button background
    neutral_btn_hover: str
    success: str
    warning: str
    danger: str
    danger_hover: str
    info: str
    selection: str       # selected-row background
    disabled_bg: str
    disabled_text: str


# The single brand accent (Toy Robot Media orange). Dark theme only.
ACCENT_ORANGE = "#ff6f3c"

# Retained for compatibility; the app now uses the single orange accent.
ACCENTS: dict[str, str] = {"Orange": ACCENT_ORANGE}


# ── Hex math helpers ─────────────────────────────────────────────────────────
def _clamp(v: int) -> int:
    return max(0, min(255, v))


def _to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(_clamp(c) for c in rgb))


def mix(a: str, b: str, t: float) -> str:
    """Linear blend of two hex colors. t=0 -> a, t=1 -> b."""
    ra, ga, ba = _to_rgb(a)
    rb, gb, bb = _to_rgb(b)
    return _to_hex((
        round(ra + (rb - ra) * t),
        round(ga + (gb - ga) * t),
        round(ba + (bb - ba) * t),
    ))


def lighten(h: str, t: float) -> str:
    return mix(h, "#ffffff", t)


def darken(h: str, t: float) -> str:
    return mix(h, "#000000", t)


def _relative_luminance(h: str) -> float:
    """WCAG 2.x relative luminance — sRGB channels are gamma-EXPANDED to linear
    first (the previous version skipped this, mis-ranking mid-tones like orange)."""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(c) for c in _to_rgb(h))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast_ratio(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def best_text_on(bg: str) -> str:
    """Pick the foreground (near-black or white) with the higher WCAG contrast on
    ``bg`` — correct for any accent. The old luminance>0.55 threshold returned
    white on the brand orange at only 2.77:1 (fails AA); this picks ~7:1 instead."""
    black, white = "#0b0e14", "#ffffff"
    return black if _contrast_ratio(black, bg) >= _contrast_ratio(white, bg) else white


# ── Base palettes ────────────────────────────────────────────────────────────
def _dark(accent: str) -> Palette:
    return Palette(
        mode="dark",
        window="#15171c",
        surface="#1d2027",
        surface_alt="#242832",
        surface_hover="#2b3039",
        border="#30353f",
        border_strong="#3c424d",
        text="#e6e8ec",
        text_muted="#9aa1ad",
        text_faint="#828b99",   # lightened to clear WCAG AA (4.5:1) for hints/placeholders
        accent=accent,
        accent_hover=lighten(accent, 0.12),
        accent_text=best_text_on(accent),
        neutral_btn="#2a2f39",
        neutral_btn_hover="#343a45",
        success="#3fb950",
        warning="#d8a23a",
        danger="#f4716a",       # nudged up to clear WCAG AA (was 4.43:1)
        danger_hover="#f6857f",
        info="#58a6ff",
        selection=mix(accent, "#15171c", 0.62),
        disabled_bg="#22262e",
        disabled_text="#5b626d",
    )


def _light(accent: str) -> Palette:
    return Palette(
        mode="light",
        window="#f3f5f8",
        surface="#ffffff",
        surface_alt="#eef1f5",
        surface_hover="#e6eaf0",
        border="#d6dbe2",
        border_strong="#c2c9d3",
        text="#1b1f27",
        text_muted="#5b6472",
        text_faint="#8b93a1",
        accent=accent,
        accent_hover=darken(accent, 0.08),
        accent_text=best_text_on(accent),
        neutral_btn="#e9edf2",
        neutral_btn_hover="#dde3ea",
        success="#1f9d4d",
        warning="#b8810f",
        danger="#dc3a2f",
        danger_hover="#c5311f",
        info="#1f6feb",
        selection=mix(accent, "#ffffff", 0.74),
        disabled_bg="#eceef2",
        disabled_text="#aab0ba",
    )


def build_palette(mode: str = "dark", accent: str | None = None) -> Palette:
    accent = accent or ACCENT_ORANGE
    base = _dark(accent) if mode != "light" else _light(accent)
    return base


# ── Active palette (process-wide) ────────────────────────────────────────────
# Widgets/delegates read the current palette via active_palette(); the main
# window overwrites it in _apply_theme before any panel is constructed.
_ACTIVE_PALETTE: Palette = build_palette("dark", ACCENT_ORANGE)

# Distinct mapping colours for video→material links (stripes in both lists).
LINK_COLORS = [
    "#e74c3c", "#e67e22", "#f1c40f", "#2ecc71", "#1abc9c",
    "#3498db", "#9b59b6", "#e91e63", "#00bcd4", "#8bc34a",
]


def active_palette() -> Palette:
    return _ACTIVE_PALETTE


def set_active_palette(pal: Palette) -> None:
    global _ACTIVE_PALETTE
    _ACTIVE_PALETTE = pal


def resolve_system_mode(default: str = "dark") -> str:
    """The OS appearance ('light' / 'dark') from Qt's reported colour scheme, or
    ``default`` when it's unknown or no QApplication exists yet. Lets the app
    follow the system light/dark setting (Qt 6.5+ QStyleHints.colorScheme)."""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            scheme = app.styleHints().colorScheme()
            if scheme == Qt.ColorScheme.Light:
                return "light"
            if scheme == Qt.ColorScheme.Dark:
                return "dark"
    except Exception:
        pass
    return default


# ── Stylesheet builder ───────────────────────────────────────────────────────
def stylesheet(p: Palette) -> str:
    """Build the full application QSS from a resolved palette.

    Covers every interactive widget (including the ones that used to fall back
    to unstyled native rendering): checkboxes, radios, scrollbars, table
    headers, tabs, group boxes, progress bars, spin boxes, tooltips, plus
    focus and disabled states.
    """
    return f"""
    /* ── Base ─────────────────────────────────────────────────────────── */
    QWidget {{
        background: transparent;
        color: {p.text};
        font-size: {FONT_BASE}px;
    }}
    /* Window/dialog backdrop comes AFTER the transparent QWidget rule so it
       wins (equal specificity → last rule applies); otherwise a top-level
       dialog like Properties/About paints transparent → black. */
    QMainWindow, QDialog {{ background: {p.window}; }}
    QToolTip {{
        background: {p.surface_alt};
        color: {p.text};
        border: 1px solid {p.border_strong};
        border-radius: {RADIUS_SM}px;
        padding: 4px 7px;
    }}

    /* ── Inputs ───────────────────────────────────────────────────────── */
    QLineEdit, QComboBox, QSpinBox, QTextEdit, QPlainTextEdit, QAbstractSpinBox {{
        background: {p.surface};
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        padding: 5px 8px;
        selection-background-color: {p.accent};
        selection-color: {p.accent_text};
    }}
    QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{ border-color: {p.border_strong}; }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QTextEdit:focus {{
        border: 1px solid {p.accent};
    }}
    QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
        background: {p.disabled_bg};
        color: {p.disabled_text};
        border-color: {p.border};
    }}
    QLineEdit::placeholder {{ color: {p.text_faint}; }}

    /* ── Keyboard focus — a visible accent ring on the interactive controls.
       On a fully-styled dark Qt app the OS focus rect is suppressed, so without
       this, Tab-ing through the window shows nothing (keyboard users are blind).
       Lists/tables are deliberately NOT bordered: the selection background
       already tracks the current row as you arrow through it, so an accent ring
       on the item is redundant noise — no box around a selected video/material. */
    QPushButton:focus, QToolButton:focus, QCheckBox:focus,
    QRadioButton:focus, QTabBar::tab:focus {{
        border: 1px solid {p.accent};
    }}

    QComboBox::drop-down {{
        subcontrol-origin: padding;
        subcontrol-position: center right;
        width: 22px;
        border: none;
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0; height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {p.text_muted};
        margin-right: 8px;
    }}
    QComboBox QAbstractItemView {{
        background: {p.surface};
        border: 1px solid {p.border_strong};
        border-radius: {RADIUS_SM}px;
        selection-background-color: {p.accent};
        selection-color: {p.accent_text};
        outline: none;
        padding: 2px;
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        width: 16px;
        border: none;
        background: {p.surface_alt};
    }}
    QSpinBox::up-button {{ border-top-right-radius: {RADIUS}px; }}
    QSpinBox::down-button {{ border-bottom-right-radius: {RADIUS}px; }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{ background: {p.surface_hover}; }}
    QSpinBox::up-arrow {{
        width: 0; height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-bottom: 5px solid {p.text_muted};
    }}
    QSpinBox::down-arrow {{
        width: 0; height: 0;
        border-left: 4px solid transparent;
        border-right: 4px solid transparent;
        border-top: 5px solid {p.text_muted};
    }}

    /* ── Buttons ──────────────────────────────────────────────────────── */
    QPushButton {{
        background: {p.neutral_btn};
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        padding: 6px 12px;
        color: {p.text};
        font-weight: 500;
    }}
    QPushButton:hover {{ background: {p.neutral_btn_hover}; border-color: {p.border_strong}; }}
    QPushButton:pressed {{ background: {darken(p.neutral_btn, 0.08) if p.mode=='dark' else p.surface_alt}; }}
    QPushButton:disabled {{
        background: {p.disabled_bg};
        color: {p.disabled_text};
        border-color: {p.border};
    }}

    QPushButton#PrimaryButton {{
        background: {p.accent};
        border: 1px solid {p.accent};
        color: {p.accent_text};
        font-weight: 600;
    }}
    QPushButton#PrimaryButton:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
    QPushButton#PrimaryButton:pressed {{ background: {darken(p.accent, 0.1)}; }}
    QPushButton#PrimaryButton:disabled {{
        background: {p.disabled_bg}; color: {p.disabled_text}; border-color: {p.border};
    }}

    QPushButton#DangerButton {{
        background: transparent;
        border: 1px solid {p.danger};
        color: {p.danger};
        font-weight: 600;
    }}
    QPushButton#DangerButton:hover {{ background: {p.danger}; color: #ffffff; }}
    QPushButton#DangerButton:pressed {{ background: {p.danger_hover}; color: #ffffff; }}
    QPushButton#DangerButton:disabled {{
        background: transparent; color: {p.disabled_text}; border-color: {p.border};
    }}

    QPushButton#IconButton {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        padding: 4px;
    }}
    QPushButton#IconButton:hover {{ background: {p.surface_hover}; border-color: {p.accent}; }}
    QPushButton#IconButton:checked {{ background: {p.accent}; border-color: {p.accent}; }}
    QPushButton#IconButton:checked:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}

    QPushButton#SmallButton {{
        padding: 2px 9px;
        font-size: {FONT_SM}px;
        border-radius: {RADIUS_SM}px;
    }}
    QPushButton#SmallButton:checked {{
        background: {p.accent}; color: {p.accent_text}; border-color: {p.accent};
    }}
    QPushButton#SmallButton:checked:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}

    /* ── Labels ───────────────────────────────────────────────────────── */
    QLabel#SectionLabel {{
        color: {p.text_muted};
        font-size: {FONT_XS}px;
        font-weight: 700;
        letter-spacing: 1px;
        margin-top: 5px;
        margin-bottom: 1px;
    }}
    QLabel#FieldLabel {{ color: {p.text_muted}; font-size: {FONT_XS}px; }}
    QLabel#HintLabel {{ color: {p.text_faint}; font-size: {FONT_XS}px; }}
    QLabel#DialogSection {{
        color: {p.accent};
        font-weight: 700;
        font-size: {FONT_SM}px;
        margin-top: 10px;
    }}

    /* ── Lists & tables ───────────────────────────────────────────────── */
    QListWidget, QTableWidget, QTreeWidget {{
        background: {p.surface};
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        padding: 2px;
        outline: none;
        alternate-background-color: {p.surface_alt};
    }}
    QListWidget::item {{ padding: 4px 6px; border-radius: {RADIUS_SM}px; }}
    QListWidget::item:hover {{ background: {p.surface_hover}; }}
    QListWidget::item:selected {{ background: {p.selection}; color: {p.text}; }}
    QTableWidget {{ gridline-color: {p.border}; }}
    QTableWidget::item {{ padding: 3px 5px; }}
    QTableWidget::item:selected {{ background: {p.selection}; color: {p.text}; }}
    QTableWidget::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {p.border_strong};
        border-radius: {RADIUS_SM}px;
        background: {p.surface};
    }}
    QTableWidget::indicator:checked {{
        background: {p.accent};
        border-color: {p.accent};
        image: none;
    }}
    QTableWidget::indicator:unchecked:hover {{ border-color: {p.accent}; }}
    QHeaderView::section {{
        background: {p.surface_alt};
        color: {p.text_muted};
        border: none;
        border-bottom: 1px solid {p.border_strong};
        padding: 5px 6px;
        font-size: {FONT_SM}px;
        font-weight: 600;
    }}
    QTableCornerButton::section {{ background: {p.surface_alt}; border: none; }}

    /* ── Checkboxes & radios ──────────────────────────────────────────── */
    QCheckBox, QRadioButton {{ spacing: 7px; }}
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 16px; height: 16px;
        border: 1px solid {p.border_strong};
        background: {p.surface};
    }}
    QCheckBox::indicator {{ border-radius: {RADIUS_SM}px; }}
    QRadioButton::indicator {{ border-radius: 8px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {p.accent}; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {p.accent};
        border-color: {p.accent};
        image: none;
    }}
    QCheckBox#ToggleHeader {{ font-weight: 700; font-size: {FONT_BASE}px; margin-bottom: 4px; }}

    /* ── Scrollbars ───────────────────────────────────────────────────── */
    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
    QScrollBar::handle:vertical {{
        background: {p.border_strong}; border-radius: 5px; min-height: 28px;
    }}
    QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 2px; }}
    QScrollBar::handle:horizontal {{
        background: {p.border_strong}; border-radius: 5px; min-width: 28px;
    }}
    QScrollBar::handle:horizontal:hover {{ background: {p.text_faint}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    /* ── Sliders ──────────────────────────────────────────────────────── */
    QSlider::groove:horizontal {{
        height: 6px; border-radius: 3px; background: {p.border};
    }}
    QSlider::sub-page:horizontal {{ background: {p.accent}; border-radius: 3px; }}
    QSlider::add-page:horizontal {{ background: {p.border}; border-radius: 3px; }}
    QSlider::handle:horizontal {{
        background: {p.accent}; border: 3px solid {p.surface_alt};
        width: 13px; height: 13px; margin: -6px 0; border-radius: 10px;
    }}
    QSlider::handle:horizontal:hover {{ background: {p.accent_hover}; }}
    QSlider::handle:horizontal:disabled {{ background: {p.border_strong}; border-color: {p.surface_alt}; }}
    QSlider::sub-page:horizontal:disabled {{ background: {p.border_strong}; }}

    /* ── Preview scrubber bar ─────────────────────────────────────────── */
    QFrame#ScrubBar {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
    }}
    QFrame#ScrubBar:disabled {{ background: {p.surface}; }}
    QSpinBox#FrameSpin {{
        background: {p.surface};
        border: 1px solid {p.border_strong};
        border-radius: {RADIUS_SM}px;
        padding: 2px 4px;
        font-weight: 700;
        color: {p.accent};
    }}
    QSpinBox#FrameSpin:disabled {{ color: {p.text_faint}; }}

    /* ── Progress bar ─────────────────────────────────────────────────── */
    QProgressBar {{
        background: {p.surface_alt};
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        text-align: center;
        color: {p.text};
        font-size: {FONT_SM}px;
        height: 18px;
    }}
    QProgressBar::chunk {{
        background: {p.accent};
        border-radius: {RADIUS - 1}px;
    }}

    /* ── Tabs ─────────────────────────────────────────────────────────── */
    /* The tab strip (incl. empty space beside the tabs) is the panel colour, so
       a solo tab and a multi-tab group share the same backdrop. */
    QTabBar {{ background: {p.window}; qproperty-drawBase: 0; }}
    /* Always start the tabs flush at the left edge (macOS centres them natively). */
    QTabWidget::tab-bar {{ alignment: left; left: 0px; top: 0px; }}
    /* Inactive tabs are flat and recede into the strip (panel colour). */
    QTabBar::tab {{
        background: {p.window};
        color: {p.text_muted};
        padding: 7px 16px;
        min-height: 18px;
        border: 1px solid {p.border};
        border-top: 2px solid transparent;
        border-bottom: none;
        border-top-left-radius: {RADIUS}px;
        border-top-right-radius: {RADIUS}px;
        margin: 0px 2px 0px 0px;
    }}
    /* Active tab is a raised, brighter chip marked by an accent top edge. */
    QTabBar::tab:selected {{
        background: {p.surface_alt};
        color: {p.text};
        border-top: 2px solid {p.accent};
    }}
    QTabBar::tab:hover {{ color: {p.text}; }}

    /* ── Group boxes ──────────────────────────────────────────────────── */
    QGroupBox {{
        border: 1px solid {p.border};
        border-radius: {RADIUS}px;
        margin-top: 10px;
        padding: 10px 8px 8px 8px;
        font-weight: 600;
    }}
    QGroupBox::title {{
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 4px;
        color: {p.text_muted};
    }}

    /* ── Dock widgets ─────────────────────────────────────────────────── */
    QDockWidget {{
        titlebar-close-icon: none;
        titlebar-normal-icon: none;
        font-weight: 600;
        font-size: {FONT_SM}px;
        color: {p.text_muted};
    }}
    QDockWidget::title {{
        text-align: left;
        background: {p.window};
        padding: 6px 8px;
        margin: 0;
        border: none;
    }}
    /* Panel body uses the active-tab colour so the active/solo tab merges
       seamlessly into its panel. */
    QDockWidget > QWidget {{ background: {p.surface_alt}; }}

    /* Solo-tab title bar for a standalone dock — looks like one selected tab,
       left-aligned, so it matches the tabs of a tabbed group. */
    #SoloTabBar {{ background: {p.window}; }}
    #SoloTab {{
        background: {p.surface_alt};
        color: {p.text};
        padding: 5px 14px;
        border: 1px solid {p.border};
        border-top: 2px solid {p.accent};
        border-bottom: none;
        border-top-left-radius: {RADIUS}px;
        border-top-right-radius: {RADIUS}px;
        font-weight: 600;
        font-size: {FONT_SM}px;
    }}

    /* Seamless dock separators — blend into the window (kills the stray
       white strokes), highlight only on hover/drag. */
    QMainWindow::separator {{
        background: {p.window};
        width: 6px;
        height: 6px;
        margin: 0;
    }}
    QMainWindow::separator:hover {{ background: {p.accent}; }}

    /* Tabbed dock bar (e.g. Queue / Live Preview) */
    QTabWidget::pane {{ border: none; }}

    /* ── Menus ────────────────────────────────────────────────────────── */
    QMenuBar {{ background: {p.surface_alt}; color: {p.text}; }}
    QMenuBar::item {{ background: transparent; padding: 5px 11px; }}
    QMenuBar::item:selected {{ background: {p.surface_hover}; border-radius: {RADIUS_SM}px; }}
    QMenu {{
        background: {p.surface};
        color: {p.text};
        border: 1px solid {p.border_strong};
        border-radius: {RADIUS_SM}px;
        padding: 4px;
    }}
    QMenu::item {{ padding: 5px 22px 5px 14px; border-radius: {RADIUS_SM}px; }}
    QMenu::item:selected {{ background: {p.accent}; color: {p.accent_text}; }}
    QMenu::separator {{ height: 1px; background: {p.border}; margin: 4px 6px; }}

    /* ── Misc ─────────────────────────────────────────────────────────── */
    QToolBar {{ background: {p.window}; border: none; border-bottom: 1px solid {p.border}; spacing: 6px; padding: 5px 4px; }}
    QSplitter::handle {{ background: {p.window}; }}
    QSplitter::handle:hover {{ background: {p.accent}; }}
    """
