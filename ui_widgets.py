"""List widgets and item delegates shared by the app's panels.

Extracted from app_qt.py. The colour stripe painted by these delegates is the
mapping/render-target indicator; the speaker badge handles per-clip audio mute.
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QByteArray, QEvent, QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLineEdit,
    QListWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QWidget,
)

import icons
import theme as T
from core.utils import SCENE_EXTENSIONS
from theme import active_palette

# Item-data roles for the material / video lists.
ROLE_VIDEO_PATH = Qt.ItemDataRole.UserRole          # absolute video path (existing)
ROLE_HAS_AUDIO = Qt.ItemDataRole.UserRole + 1       # bool: clip carries an audio stream
ROLE_MUTED = Qt.ItemDataRole.UserRole + 2           # bool: user muted this clip's audio
ROLE_MAP_COLOR = Qt.ItemDataRole.UserRole + 3       # str hex: mapping colour, or None
ROLE_TARGET = Qt.ItemDataRole.UserRole + 5          # bool: material is a render target

_AUDIO_BADGE_PX = 14                    # logical size of the speaker glyph
_AUDIO_BADGE_MARGIN = 6                # inset from the left row edge — clears the 3px stripe (ends at x≈5)
_AUDIO_TEXT_INDENT = 17               # fixed slot reserved on every row so text always aligns


class MappingStripeDelegate(QStyledItemDelegate):
    """Base list delegate that draws a thin colour stripe at the left edge for
    a mapped row (ROLE_MAP_COLOR) — it sits in the row's left margin so the
    text never shifts and stays aligned with unmapped rows. Also washes the row
    in accent when ``panel`` reports it as cross-highlighted (the partner of the
    hovered/selected row in the other list)."""

    # The left-edge indicator language (shared by both lists):
    #   • FILLED solid colour  → a clip is linked (the colour identifies the pair)
    #   • STROKED outline      → reserved / pending (a render target with no clip,
    #                            or a hover affordance) — same shape, hollow.
    # Fill = done, stroke = waiting; so the two never read as the same thing, and
    # 'pending' no longer borrows the vivid mapping colours or the focus accent.
    _STRIPE_W = 4

    def __init__(self, panel, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._panel = panel
        self._kind = kind  # "material" | "video"

    def _item_key(self, index):
        if self._kind == "video":
            return index.data(ROLE_VIDEO_PATH)
        return index.data(Qt.ItemDataRole.DisplayRole)

    def _paint_cross_highlight(self, painter, option, index) -> None:
        panel = self._panel
        if panel is not None and panel._is_cross_highlighted(self._kind, self._item_key(index)):
            c = QColor(active_palette().accent)
            c.setAlpha(46)
            painter.save()
            painter.fillRect(option.rect, c)
            painter.restore()

    def _bar_rect(self, option) -> QRect:
        r = option.rect
        return QRect(r.left() + 2, r.top() + 4, self._STRIPE_W, max(0, r.height() - 8))

    def _draw_bar(self, painter, bar: QRect, color, *, filled: bool, alpha: int = 255) -> None:
        c = QColor(color)
        c.setAlpha(alpha)
        rad = self._STRIPE_W / 2
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if filled:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(c)
            painter.drawRoundedRect(bar, rad, rad)
        else:                                   # stroked outline → reserved / pending
            pen = QPen(c)
            pen.setWidthF(1.3)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(bar).adjusted(0.65, 0.65, -0.65, -0.65), rad, rad)
        painter.restore()

    def _paint_stripe(self, painter, option, index) -> None:
        color = index.data(ROLE_MAP_COLOR)
        if color:                               # a clip is linked → filled colour
            self._draw_bar(painter, self._bar_rect(option), color, filled=True)

    def paint(self, painter, option, index) -> None:  # type: ignore[override]
        self._paint_cross_highlight(painter, option, index)
        super().paint(painter, option, index)
        self._paint_stripe(painter, option, index)


class TargetStripeDelegate(MappingStripeDelegate):
    """Material-list delegate. The left stripe IS the render-target indicator:
      • colourful  → a clip is linked (and the material is a render target)
      • outline    → marked as a target, waiting for a clip
      • ghost      → shown on hover, so clicking the stripe marks it a target
    Clicking the left stripe zone toggles the target; right-click does too."""

    _HIT_W = 16   # clickable zone on the left edge

    def __init__(self, toggle_cb, panel, parent: QWidget | None = None) -> None:
        super().__init__(panel, "material", parent)
        self._toggle = toggle_cb

    def _paint_stripe(self, painter, option, index) -> None:
        color = index.data(ROLE_MAP_COLOR)
        targeted = bool(index.data(ROLE_TARGET))
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if not (color or targeted or hovered):
            return
        bar = self._bar_rect(option)
        pal = active_palette()
        if color:                                   # clip linked → FILLED mapping colour
            self._draw_bar(painter, bar, color, filled=True)
        elif targeted:                              # render target, no clip yet → accent OUTLINE
            self._draw_bar(painter, bar, pal.accent, filled=False)
        else:                                       # hover: could become a target → faint ghost OUTLINE
            self._draw_bar(painter, bar, pal.text_faint, filled=False, alpha=150)

    def editorEvent(self, event, model, option, index) -> bool:  # type: ignore[override]
        if (event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton
                and (event.position().toPoint().x() - option.rect.left()) <= self._HIT_W):
            mat = index.data(Qt.ItemDataRole.DisplayRole)
            if mat and self._toggle:
                self._toggle(mat)
            return True                             # swallow so the row isn't toggled-selected
        return super().editorEvent(event, model, option, index)


class AudioBadgeDelegate(MappingStripeDelegate):
    """Video-list delegate: the mapping stripe plus a clickable speaker badge on
    the left of any row whose clip has audio. Clicking the badge toggles that
    clip's mute state; a muted clip shows a struck-through speaker."""

    def __init__(self, toggle_cb, panel, parent: QWidget | None = None) -> None:
        super().__init__(panel, "video", parent)
        self._toggle = toggle_cb
        # Path of the row whose badge the cursor is currently over, set by the
        # owning VideoListWidget so paint() can give it a hover affordance.
        self._hover_badge: str | None = None

    @staticmethod
    def _badge_rect(item_rect: QRect) -> QRect:
        size = _AUDIO_BADGE_PX
        x = item_rect.left() + _AUDIO_BADGE_MARGIN
        y = item_rect.center().y() - size // 2 + 1
        return QRect(x, y, size, size)

    def _paint_row_background(self, painter, option) -> None:
        """Fill the full row width with the hover / selection colour so the band
        sits *behind* the badge slot too — the default delegate would only paint
        from the indented text edge, leaving the badge floating outside it."""
        st = option.state
        pal = active_palette()
        if st & QStyle.StateFlag.State_Selected:
            color = QColor(pal.selection)
        elif st & QStyle.StateFlag.State_MouseOver:
            color = QColor(pal.surface_hover)
        else:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(option.rect, T.RADIUS_SM, T.RADIUS_SM)
        painter.restore()

    def paint(self, painter, option, index) -> None:  # type: ignore[override]
        self._paint_cross_highlight(painter, option, index)
        # Paint the hover/selection band across the whole row first so it reads
        # as one interactive strip (badge included), then draw the label in the
        # indented text slot. All rows share the same indent so text aligns
        # whether or not a badge is present.
        self._paint_row_background(painter, option)
        has_audio = bool(index.data(ROLE_HAS_AUDIO))
        opt = QStyleOptionViewItem(option)
        opt.rect = QRect(option.rect)
        opt.rect.setLeft(opt.rect.left() + _AUDIO_TEXT_INDENT)
        QStyledItemDelegate.paint(self, painter, opt, index)
        self._paint_stripe(painter, option, index)
        if has_audio:
            muted = bool(index.data(ROLE_MUTED))
            pal = active_palette()
            badge_r = self._badge_rect(option.rect)
            hovered = self._hover_badge is not None and self._hover_badge == index.data(ROLE_VIDEO_PATH)
            if hovered:
                # Accent-tinted chip behind the speaker so it clearly reads as a
                # clickable toggle on hover.
                chip = QColor(pal.accent)
                chip.setAlpha(60)
                painter.save()
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(chip)
                painter.drawRoundedRect(badge_r.adjusted(-3, -2, 3, 2), 5, 5)
                painter.restore()
            color = (pal.text_muted if hovered else pal.text_faint) if muted else pal.accent
            pm = icons.pixmap("volume_x" if muted else "volume", color, _AUDIO_BADGE_PX)
            painter.drawPixmap(badge_r.topLeft(), pm)

    def editorEvent(self, event, model, option, index) -> bool:  # type: ignore[override]
        # Intercept press/release/double-click on the badge so the click toggles
        # mute without the list treating it as a (de)selection of the row.
        badge_events = (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonRelease,
            QEvent.Type.MouseButtonDblClick,
        )
        if (
            bool(index.data(ROLE_HAS_AUDIO))
            and event.type() in badge_events
            and event.button() == Qt.MouseButton.LeftButton
            and self._badge_rect(option.rect).contains(event.position().toPoint())
        ):
            if event.type() == QEvent.Type.MouseButtonRelease:
                path = index.data(ROLE_VIDEO_PATH)
                if path and self._toggle:
                    self._toggle(path)
            return True  # swallow press/dblclick too → selection is untouched
        return super().editorEvent(event, model, option, index)


class _ImageView(QWidget):
    """Paints a pixmap directly in paintEvent over an opaque background — bypasses
    a Qt quirk where a QLabel's pixmap (or a translucent widget) can fail to
    composite while a global stylesheet is active."""

    def __init__(self, pixmap: QPixmap, bg: str | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pm = pixmap
        self._bg = QColor(bg) if bg else None
        # Size to the device-independent (logical) size so a Retina/2x pixmap
        # isn't drawn into an oversized box (which would push it off-centre).
        self.setFixedSize(pixmap.deviceIndependentSize().toSize())

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        if self._bg is not None:
            painter.fillRect(self.rect(), self._bg)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(0, 0, self._pm)


# Mime type carrying dragged clip path(s), so a clip can be dragged out of the
# video list and dropped onto a material to map it (distinct from a Finder file
# drop, which arrives as text/uri-list).
CLIP_MIME = "application/x-rmp-clip"


class VideoListWidget(QListWidget):
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # External Finder drops reach our viewport event filter; DragOnly makes
        # this list a drag SOURCE (never an internal drop target) so a clip can be
        # dragged onto a material row to map it.
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragOnly)
        self.setDragEnabled(True)
        self.setAcceptDrops(False)
        self.setMouseTracking(True)
        vp = self.viewport()
        vp.setMouseTracking(True)
        vp.setAcceptDrops(True)
        vp.installEventFilter(self)

    def mimeData(self, items):  # type: ignore[override]
        """Carry the dragged clip path(s) for a drop onto a material."""
        md = super().mimeData(items)
        paths = [str(it.data(ROLE_VIDEO_PATH)) for it in items
                 if it.data(ROLE_VIDEO_PATH)
                 and not str(it.data(ROLE_VIDEO_PATH)).startswith("__add_video__")]
        if paths:
            joined = "\n".join(paths)
            md.setText(joined)
            md.setData(CLIP_MIME, QByteArray(joined.encode("utf-8")))
        return md

    def _update_badge_hover(self, pos) -> None:
        """Track whether the cursor is over an audio badge and tell the delegate
        so it can paint the hover affordance; also swap to a pointing cursor."""
        deleg = self.itemDelegate()
        if not isinstance(deleg, AudioBadgeDelegate):
            return
        new_path = None
        idx = self.indexAt(pos)
        if (idx.isValid() and bool(idx.data(ROLE_HAS_AUDIO))
                and AudioBadgeDelegate._badge_rect(self.visualRect(idx)).contains(pos)):
            new_path = idx.data(ROLE_VIDEO_PATH)
        if new_path != deleg._hover_badge:
            deleg._hover_badge = new_path
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if new_path else Qt.CursorShape.ArrowCursor)
            self.viewport().update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor(active_palette().text_faint))
            painter.drawText(
                self.viewport().rect().adjusted(12, 0, -12, 0),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "Drag & drop videos or images here\n(or click Add)",
            )
            painter.end()

    @staticmethod
    def _paths_from_event(event) -> list[str]:
        md = event.mimeData() if hasattr(event, "mimeData") else None
        if not md:
            return []
        paths: list[str] = []
        if md.hasUrls():
            for u in md.urls():
                if u.isLocalFile():
                    p = u.toLocalFile()
                    if p:
                        paths.append(p)
        if not paths and md.hasText():
            for raw in md.text().splitlines():
                s = raw.strip().removeprefix("file://").replace("%20", " ")
                if os.path.isabs(s):
                    paths.append(s)
        return paths

    def eventFilter(self, watched, event):  # type: ignore[override]
        try:
            vp = super().viewport()
        except Exception:
            return super().eventFilter(watched, event)
        if watched is vp:
            t = event.type()
            if t in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                if self._paths_from_event(event):
                    event.acceptProposedAction()
                    return True
            elif t == QEvent.Type.Drop:
                paths = self._paths_from_event(event)
                if paths:
                    self.files_dropped.emit(paths)
                    event.acceptProposedAction()
                    return True
            elif t == QEvent.Type.MouseMove:
                self._update_badge_hover(event.position().toPoint())
            elif t == QEvent.Type.Leave:
                self._update_badge_hover(QPoint(-1, -1))
        return super().eventFilter(watched, event)


class ScenePathLineEdit(QLineEdit):
    file_dropped = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _extract_scene_file_path(event) -> str:
        md = event.mimeData()
        if not md or not md.hasUrls():
            return ""
        for u in md.urls():
            if not u.isLocalFile():
                continue
            p = Path(u.toLocalFile()).expanduser()
            if p.suffix.lower() in SCENE_EXTENSIONS:
                return str(p)
        return ""

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if self._extract_scene_file_path(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._extract_scene_file_path(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        path = self._extract_scene_file_path(event)
        if path:
            self.file_dropped.emit(path)
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class MaterialListWidget(QListWidget):
    """Materials list that also accepts a dropped scene file and shows a
    drag-drop hint while empty."""

    scene_dropped = Signal(str)
    clip_dropped = Signal(str, str)   # (material_name, clip_path) — drag a clip here to map it

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    @staticmethod
    def _scene_path(event) -> str:
        md = event.mimeData()
        if not md or not md.hasUrls():
            return ""
        for u in md.urls():
            if u.isLocalFile():
                p = Path(u.toLocalFile())
                if p.suffix.lower() in SCENE_EXTENSIONS:
                    return str(p)
        return ""

    @staticmethod
    def _clip_path(event) -> str:
        md = event.mimeData()
        if md and md.hasFormat(CLIP_MIME):
            raw = bytes(md.data(CLIP_MIME).data()).decode("utf-8", "ignore")
            for line in raw.splitlines():
                if line.strip():
                    return line.strip()
        return ""

    def _item_at(self, event):
        try:
            pos = event.position().toPoint()
        except Exception:
            pos = event.pos()
        return self.itemAt(pos)

    def dragEnterEvent(self, event) -> None:  # type: ignore[override]
        if self._clip_path(event) or self._scene_path(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._clip_path(event):
            it = self._item_at(event)
            if it is not None:
                self.setCurrentItem(it)   # highlight the material the clip will map to
                event.acceptProposedAction()
                return
            event.ignore()
            return
        if self._scene_path(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # type: ignore[override]
        clip = self._clip_path(event)
        if clip:
            it = self._item_at(event)
            if it is not None:
                self.clip_dropped.emit(it.text(), clip)
                event.acceptProposedAction()
                return
            event.ignore()
            return
        path = self._scene_path(event)
        if path:
            self.scene_dropped.emit(path)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self.viewport())
            painter.setPen(QColor(active_palette().text_faint))
            painter.drawText(
                self.viewport().rect().adjusted(12, 0, -12, 0),
                Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap,
                "Drag & drop your scene here\n(.glb, .blend, .fbx…)",
            )
            painter.end()




class HintListWidget(QListWidget):
    """QListWidget that paints a muted call-to-action hint while empty."""

    def __init__(self, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._hint = hint

    def set_hint(self, hint: str) -> None:
        self._hint = hint
        self.viewport().update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self.count() == 0 and self._hint:
            painter = QPainter(self.viewport())
            painter.setPen(QColor(active_palette().text_faint))
            painter.drawText(self.viewport().rect().adjusted(12, 0, -12, 0),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._hint)
            painter.end()


class HintTableWidget(QTableWidget):
    """QTableWidget that paints a muted call-to-action hint while empty."""

    def __init__(self, rows: int, cols: int, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(rows, cols, parent)
        self._hint = hint

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self.rowCount() == 0 and self._hint:
            painter = QPainter(self.viewport())
            painter.setPen(QColor(active_palette().text_faint))
            painter.drawText(self.viewport().rect().adjusted(16, 0, -16, 0),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._hint)
            painter.end()
