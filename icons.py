"""Unified, theme-tinted icon set for Render Mapper Pro.

One coherent stroke-based icon family (Lucide-style: 24x24 viewBox, 2px round
stroke) rendered to QIcon at any color/size. Replaces the previous mix of
hand-painted QPainter icons, a lone native QStyle icon, and unicode glyphs
used as button labels.

    from icons import icon, app_icon
    btn.setIcon(icon("play", palette.accent_text))
"""

from __future__ import annotations

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Inner SVG body for each icon. Stroke-based unless listed in _FILLED.
# Coordinate space is the Lucide standard 24x24 viewBox.
_BODY: dict[str, str] = {
    # transport / queue controls
    "play": '<polygon points="7 4 20 12 7 20 7 4"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "skip": '<polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19"/>',
    "pause": '<rect x="6" y="5" width="4" height="14" rx="1"/><rect x="14" y="5" width="4" height="14" rx="1"/>',
    # editing
    "plus": '<line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>',
    "link": '<path d="M9 17H7A5 5 0 0 1 7 7h2"/><path d="M15 7h2a5 5 0 1 1 0 10h-2"/><line x1="8" y1="12" x2="16" y2="12"/>',
    "unlink": '<path d="M9 17H7A5 5 0 0 1 7 7h2"/><path d="M15 7h2a5 5 0 1 1 0 10h-2"/><line x1="2" y1="2" x2="22" y2="22"/>',
    "reset": '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>',
    "trash": '<polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/>',
    "copy": '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    "refresh": '<path d="M21 2v6h-6"/><path d="M3 12a9 9 0 0 1 15-6.7L21 8"/><path d="M3 22v-6h6"/><path d="M21 12a9 9 0 0 1-15 6.7L3 16"/>',
    "save": '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/>',
    # files / navigation
    "folder": '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/>',
    "open": '<path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>',
    "search": '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>',
    "chevron_down": '<polyline points="6 9 12 15 18 9"/>',
    "chevron_left": '<polyline points="15 18 9 12 15 6"/>',
    "chevron_right": '<polyline points="9 18 15 12 9 6"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/>',
    "check_apply": '<polyline points="20 6 9 17 4 12"/>',
    # domain
    "film": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="7" y1="3" x2="7" y2="21"/><line x1="17" y1="3" x2="17" y2="21"/><line x1="3" y1="7.5" x2="7" y2="7.5"/><line x1="17" y1="7.5" x2="21" y2="7.5"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="16.5" x2="7" y2="16.5"/><line x1="17" y1="16.5" x2="21" y2="16.5"/>',
    "camera": '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>',
    "clapper": '<path d="M20.2 6 3 11l-.9-2.4c-.3-1.1.3-2.2 1.3-2.5l13.5-4c1.1-.3 2.2.3 2.5 1.3Z"/><path d="m6.2 5.3 3.1 3.9"/><path d="m12.4 3.4 3.1 4"/><path d="M3 11h18v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/>',
    "sliders": '<line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/>',
    "server": '<rect x="2" y="3" width="20" height="8" rx="2"/><rect x="2" y="13" width="20" height="8" rx="2"/><line x1="6" y1="7" x2="6.01" y2="7"/><line x1="6" y1="17" x2="6.01" y2="17"/>',
    "queue": '<line x1="9" y1="6" x2="21" y2="6"/><line x1="9" y1="12" x2="21" y2="12"/><line x1="9" y1="18" x2="21" y2="18"/><circle cx="4" cy="6" r="1.4"/><circle cx="4" cy="12" r="1.4"/><circle cx="4" cy="18" r="1.4"/>',
    "bookmark": '<path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z"/>',
    "terminal": '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>',
    "palette": '<circle cx="13.5" cy="6.5" r="1.5"/><circle cx="17.5" cy="10.5" r="1.5"/><circle cx="8.5" cy="7.5" r="1.5"/><circle cx="6.5" cy="12.5" r="1.5"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.555C21.965 6.012 17.461 2 12 2z"/>',
    # status
    "check": '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>',
    "alert": '<circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>',
    "help": '<circle cx="12" cy="12" r="9"/><path d="M9.1 9.5a3 3 0 0 1 5.82 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
    "info": '<circle cx="12" cy="12" r="9"/><line x1="12" y1="11" x2="12" y2="16"/><line x1="12" y1="8" x2="12.01" y2="8"/>',
    "x_circle": '<circle cx="12" cy="12" r="9"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/>',
    "circle": '<circle cx="12" cy="12" r="8"/>',
    "x": '<line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>',
    "volume": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>',
    "volume_x": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><line x1="22" y1="9" x2="16" y2="15"/><line x1="16" y1="9" x2="22" y2="15"/>',
}

# Icons that read better filled than stroked.
_FILLED = {"play", "stop", "skip"}


_SVG_TMPL = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
    'fill="{fill}" stroke="{stroke}" stroke-width="2.1" '
    'stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
)

_cache: dict[tuple, QIcon] = {}


def _svg_pixmap(name: str, color: str, px: int) -> QPixmap:
    body = _BODY.get(name, _BODY["circle"])
    if name in _FILLED:
        svg = _SVG_TMPL.format(fill=color, stroke=color, body=body)
    else:
        svg = _SVG_TMPL.format(fill="none", stroke=color, body=body)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    # Render at 2x for crisp HiDPI, then mark device pixel ratio.
    scale = 2
    pm = QPixmap(px * scale, px * scale)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    renderer.render(painter, QRectF(0, 0, px * scale, px * scale))
    painter.end()
    pm.setDevicePixelRatio(scale)
    return pm


def icon(name: str, color: str, size: int = 18) -> QIcon:
    """Return a QIcon for ``name`` stroked/filled in ``color``."""
    key = (name, color.lower(), size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    ic = QIcon(_svg_pixmap(name, color, size))
    _cache[key] = ic
    return ic


def pixmap(name: str, color: str, size: int = 18) -> QPixmap:
    return _svg_pixmap(name, color, size)


def app_icon() -> QIcon:
    """Application/window icon: a clapperboard in the brand palette, drawn in
    the same stroke language as the rest of the icon set so the launcher badge
    matches the in-app icons."""
    px = 256
    pm = QPixmap(px, px)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # rounded badge background
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#1d2027"))
    painter.drawRoundedRect(14, 14, px - 28, px - 28, 46, 46)
    painter.end()

    # overlay a tinted clapperboard glyph centered on the badge (brand orange)
    glyph = _svg_pixmap("clapper", "#ff6f3c", 150)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    gx = (px - glyph.width() / glyph.devicePixelRatio()) / 2
    gy = (px - glyph.height() / glyph.devicePixelRatio()) / 2
    painter.drawPixmap(int(gx), int(gy), glyph)
    painter.end()
    return QIcon(pm)


def clear_cache() -> None:
    _cache.clear()
