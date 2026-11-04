"""Reusable widgets: zoomable page canvas with rectangle drawing, image helpers, workers."""
from dataclasses import dataclass
from typing import List, Optional, Tuple
import cv2
import numpy as np
from PyQt6.QtWidgets import QWidget, QScrollArea, QLabel, QSizePolicy
from PyQt6.QtCore import Qt, QRectF, QPointF, pyqtSignal, QThread
from PyQt6.QtGui import QPixmap, QImage, QPainter, QPen, QColor, QBrush, QFont

def np_to_pixmap(img: np.ndarray) -> QPixmap:
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w = rgb.shape[:2]
    qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
    return QPixmap.fromImage(qimg)

@dataclass
class Overlay:
    rect: Tuple[int, int, int, int]
    color: QColor
    text: str = ""
    selected: bool = False
    dashed: bool = False
    key: object = None   # returned by hit-testing
    editable: bool = False   # can be moved/resized once it is the active box

HANDLE = 5        # handle half-size in screen pixels
MIN_BOX = 8       # smallest box side in image pixels
# handle name -> (moves left, top, right, bottom edge)
_HANDLES = {"tl": (1, 1, 0, 0), "t": (0, 1, 0, 0), "tr": (0, 1, 1, 0), "r": (0, 0, 1, 0),
            "br": (0, 0, 1, 1), "b": (0, 0, 0, 1), "bl": (1, 0, 0, 1), "l": (1, 0, 0, 0)}
_HANDLE_CURSORS = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                   "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                   "t": Qt.CursorShape.SizeVerCursor, "b": Qt.CursorShape.SizeVerCursor,
                   "l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                   "move": Qt.CursorShape.SizeAllCursor}

class PageCanvas(QWidget):
    """Displays a page image at a zoom factor; user can drag rectangles (image coords).
    The active editable box (active_key) shows handles: drag them to resize, drag inside to
    move, arrow keys nudge. Shift+drag always draws a new box."""
    rectDrawn = pyqtSignal(tuple)
    overlayClicked = pyqtSignal(object)
    overlayEdited = pyqtSignal(object, tuple)   # key, new rect (image coords)

    def __init__(self):
        super().__init__()
        self._pix: Optional[QPixmap] = None
        self._scale = 1.0
        self.overlays: List[Overlay] = []
        self.draw_enabled = True
        self.draw_color = QColor(79, 70, 229)
        self.active_key = None
        self._start = None
        self._drag = None
        self._edit = None        # (handle name or "move", original rect) while editing
        self._edit_rect = None   # live rect while editing
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def image_size(self):
        return (self._pix.width(), self._pix.height()) if self._pix else (0, 0)

    def set_image(self, img):
        self._pix = np_to_pixmap(img) if img is not None else None
        self._update_size()

    def set_overlays(self, overlays: List[Overlay]):
        self.overlays = overlays
        self.update()

    def scale(self):
        return self._scale

    def set_scale(self, s: float):
        self._scale = max(0.05, min(s, 5.0))
        self._update_size()

    def _update_size(self):
        if self._pix:
            self.setFixedSize(int(self._pix.width() * self._scale), int(self._pix.height() * self._scale))
        else:
            self.setFixedSize(10, 10)
        self.update()

    def _to_img(self, p) -> QPointF:
        return QPointF(p.x() / self._scale, p.y() / self._scale)

    def paintEvent(self, event):
        if not self._pix:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.drawPixmap(self.rect(), self._pix)
        s = self._scale
        font = QFont(self.font()); font.setPointSize(9); font.setBold(True)
        p.setFont(font)
        active = self._active()
        for o in self.overlays:
            x, y, w, h = self._edit_rect if (o is active and self._edit_rect) else o.rect
            r = QRectF(x * s, y * s, w * s, h * s)
            fill = QColor(o.color); fill.setAlpha(55 if o.selected else 22)
            pen = QPen(o.color, 3 if o.selected else 1.6,
                       Qt.PenStyle.DashLine if o.dashed else Qt.PenStyle.SolidLine)
            p.setPen(pen); p.setBrush(QBrush(fill))
            p.drawRoundedRect(r, 3, 3)
            if o.text:
                fm = p.fontMetrics()
                tw = fm.horizontalAdvance(o.text) + 10
                tag = QRectF(r.left(), max(0, r.top() - 18), tw, 18)
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(o.color))
                p.drawRoundedRect(tag, 3, 3)
                p.setPen(QColor("white"))
                p.drawText(tag, Qt.AlignmentFlag.AlignCenter, o.text)
        if active is not None:
            p.setPen(QPen(active.color, 1.2)); p.setBrush(QBrush(QColor("white")))
            for hr in self._handle_rects(active).values():
                p.drawRect(hr)
        if self._drag is not None:
            pen = QPen(self.draw_color, 2, Qt.PenStyle.DashLine)
            fill = QColor(self.draw_color); fill.setAlpha(40)
            p.setPen(pen); p.setBrush(QBrush(fill))
            p.drawRect(self._drag)
        p.end()

    # ---- editing the active box ----
    def _active(self) -> Optional[Overlay]:
        if self.active_key is None or not self.draw_enabled:
            return None
        return next((o for o in self.overlays if o.key == self.active_key and o.editable), None)

    def _handle_rects(self, o: Overlay) -> dict:
        x, y, w, h = self._edit_rect or o.rect
        s, k = self._scale, HANDLE
        xs = {"l": x * s, "c": (x + w / 2) * s, "r": (x + w) * s}
        ys = {"t": y * s, "c": (y + h / 2) * s, "b": (y + h) * s}
        pos = {"tl": ("l", "t"), "t": ("c", "t"), "tr": ("r", "t"), "r": ("r", "c"),
               "br": ("r", "b"), "b": ("c", "b"), "bl": ("l", "b"), "l": ("l", "c")}
        return {n: QRectF(xs[a] - k, ys[b] - k, 2 * k, 2 * k) for n, (a, b) in pos.items()}

    def _edit_target(self, pos) -> Optional[str]:
        """Handle name, 'move' (inside the active box) or None."""
        o = self._active()
        if o is None:
            return None
        for n, hr in self._handle_rects(o).items():
            if hr.adjusted(-2, -2, 2, 2).contains(pos):
                return n
        x, y, w, h = o.rect
        pt = self._to_img(pos)
        return "move" if x <= pt.x() <= x + w and y <= pt.y() <= y + h else None

    def _clamp(self, x0, y0, x1, y1) -> Tuple[int, int, int, int]:
        W, H = self.image_size()
        x0, x1 = sorted((x0, x1)); y0, y1 = sorted((y0, y1))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(W, max(x1, x0 + MIN_BOX)), min(H, max(y1, y0 + MIN_BOX))
        return int(x0), int(y0), int(x1 - x0), int(y1 - y0)

    def _edited_rect(self, pos):
        mode, (x, y, w, h) = self._edit
        d = self._to_img(pos) - self._to_img(self._start)
        dx, dy = d.x(), d.y()
        if mode == "move":
            W, H = self.image_size()
            nx, ny = min(max(0, x + dx), W - w), min(max(0, y + dy), H - h)
            return int(nx), int(ny), w, h
        ml, mt, mr, mb = _HANDLES[mode]
        return self._clamp(x + dx * ml, y + dy * mt, x + w + dx * mr, y + h + dy * mb)

    def _commit_edit(self, rect):
        o = self._active()
        if o is not None and tuple(rect) != tuple(o.rect):
            o.rect = tuple(rect)
            self.overlayEdited.emit(o.key, tuple(rect))
        self.update()

    def keyPressEvent(self, e):
        o = self._active()
        steps = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}
        if o is None or e.key() not in steps:
            return super().keyPressEvent(e)
        n = 10 if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        dx, dy = steps[e.key()]
        x, y, w, h = o.rect
        W, H = self.image_size()
        self._commit_edit((min(max(0, x + dx * n), W - w), min(max(0, y + dy * n), H - h), w, h))

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self._pix:
            self._start = e.position()
            self._drag = None
            target = None if e.modifiers() & Qt.KeyboardModifier.ShiftModifier else self._edit_target(e.position())
            self._edit = (target, self._active().rect) if target else None
            self._edit_rect = None

    def mouseMoveEvent(self, e):
        if self._start is None:
            if self.draw_enabled:
                t = self._edit_target(e.position())
                self.setCursor(_HANDLE_CURSORS[t] if t else Qt.CursorShape.CrossCursor)
            return
        if self._edit is not None:
            self._edit_rect = self._edited_rect(e.position())
            self.update()
        elif self.draw_enabled:
            self._drag = QRectF(self._start, e.position()).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if self._start is None:
            return
        if self._edit is not None:
            rect = self._edit_rect
            self._edit, self._edit_rect = None, None
            if rect is not None:
                self._start = None
                self._commit_edit(rect)
                return
            # no movement: a plain click (may select a smaller box nested in the active one)
        drag, start = self._drag, self._start
        self._start, self._drag = None, None
        self.update()
        if drag is not None and drag.width() > 6 and drag.height() > 6:
            s = self._scale
            self.rectDrawn.emit((int(drag.x() / s), int(drag.y() / s), int(drag.width() / s), int(drag.height() / s)))
            return
        # treat as click: hit-test overlays (smallest first so nested boxes are selectable)
        pt = self._to_img(start)
        hits = [o for o in self.overlays if o.key is not None
                and o.rect[0] <= pt.x() <= o.rect[0] + o.rect[2] and o.rect[1] <= pt.y() <= o.rect[1] + o.rect[3]]
        if hits:
            self.overlayClicked.emit(min(hits, key=lambda o: o.rect[2] * o.rect[3]).key)

class PageViewer(QScrollArea):
    """Scrollable PageCanvas with fit-to-width and Ctrl+wheel zoom."""
    def __init__(self):
        super().__init__()
        self.setObjectName("canvasScroll")
        self.canvas = PageCanvas()
        self.setWidget(self.canvas)
        self.setWidgetResizable(False)
        self.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self._fit = True

    def set_image(self, img):
        self.canvas.set_image(img)
        self.fit_width()

    def fit_width(self):
        self._fit = True
        w, _ = self.canvas.image_size()
        if w:
            self.canvas.set_scale((self.viewport().width() - 16) / w)

    def zoom(self, factor: float):
        self._fit = False
        self.canvas.set_scale(self.canvas.scale() * factor)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._fit:
            self.fit_width()

    def wheelEvent(self, e):
        if e.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 if e.angleDelta().y() > 0 else 1 / 1.15)
            e.accept()
        else:
            super().wheelEvent(e)

class ImageThumb(QLabel):
    """Shows an image file scaled to the label width."""
    def __init__(self, max_height=260):
        super().__init__()
        self._pix = None
        self._max_h = max_height
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setObjectName("thumb")      # styled in theme (light/dark)

    def set_image(self, img):
        self._pix = np_to_pixmap(img) if img is not None else None
        self._rescale()

    def _rescale(self):
        if self._pix is None:
            self.setText("(no image)")
            return
        pm = self._pix.scaled(max(50, self.width() - 4), self._max_h, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        self.setPixmap(pm)
        self.setMinimumHeight(pm.height() + 4)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._pix is not None:
            self._rescale()

class Worker(QThread):
    """Runs fn(*args, progress=emit) in a thread. fn may call progress(i, total, message)."""
    progress = pyqtSignal(int, int, str)
    item = pyqtSignal(object)        # partial results
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, *args):
        super().__init__()
        self.fn, self.args = fn, args
        self.cancelled = False
        self.llm = None   # jobs may set worker.llm so stop() can abort the in-flight request

    def stop(self):
        """Request a stop. Jobs check `worker.cancelled` between steps; an LLM request that is
        already running is aborted by closing its HTTP client (the job then ends with an error
        that callers should treat as 'stopped'). Results already saved are kept."""
        self.cancelled = True
        llm = self.llm
        if llm is not None:
            try:
                llm.abort()
            except Exception:
                pass

    def run(self):
        try:
            res = self.fn(*self.args, worker=self)
            self.done.emit(res)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.failed.emit(str(e))
