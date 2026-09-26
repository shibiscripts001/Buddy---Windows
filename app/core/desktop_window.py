#!/usr/bin/env python3
"""
The desktop layout (a theme with "layout": "desktop"): tools as floating
windows on a dotted desk, instead of the rail's panes. core/desktop_layout.py
decides where everything goes; this draws it and turns the mouse into
those decisions.

Like the pane divider this is the window frame itself, so it's Qt: a title
bar in one of the palette's window colours, working minimise / maximise /
close buttons, dragging by the title, resizing from the edges and corners,
and a click anywhere in a window bringing it to the front. The page inside
is the same ToolPage the panes show. Closing or minimising a window only
hides it, page and all, so a tool keeps running (Time Tracker keeps
tracking).

Each window is a native child window, clipped to its shape. Chromium
draws a page as a texture that Qt composites in its own order - and one
page can be flagged to always stack on top - so plain Qt widgets can't
keep overlapping web pages in order. Native windows are stacked by
Windows itself. Anything that must draw over them is native too (the busy
overlay) or a window of its own (the taskbar's menus).

A page only ever moves straight from one parent to another - never out to
no parent at all (QScrollArea.takeWidget() does that), which would make it
a top-level window of its own for a moment.
"""

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QFrame, QScrollArea, QWidget

from core import desktop_layout as dl
from core.theme import _blend, desktop_colors, ensure_contrast

SHADOW = 5          # the hard offset shadow, right and down (and extra grab room)
RADIUS = 12
BORDER = 2
TITLE_H = 34
INSET = 8           # page edge to frame edge: room to grab, and square page corners inside the curve
GRIP = INSET        # how close to an edge the pointer resizes: the frame, not the page
CORNER = 18         # how far along an edge a corner reaches - bigger, so it's easy to catch
BTN = 20            # title-bar button size
BTN_GAP = 6
DOT_STEP = 16       # the desk's dot grid

# Edges a resize drags, by where the press landed.
_CURSORS = {
    frozenset({"left"}): Qt.SizeHorCursor, frozenset({"right"}): Qt.SizeHorCursor,
    frozenset({"top"}): Qt.SizeVerCursor, frozenset({"bottom"}): Qt.SizeVerCursor,
    frozenset({"top", "left"}): Qt.SizeFDiagCursor, frozenset({"bottom", "right"}): Qt.SizeFDiagCursor,
    frozenset({"top", "right"}): Qt.SizeBDiagCursor, frozenset({"bottom", "left"}): Qt.SizeBDiagCursor,
}


def _families(css_font):
    """A CSS font-family list as the names QFont wants."""
    return [f.strip().strip('"').strip("'") for f in css_font.split(",")
            if f.strip() and f.strip() not in ("sans-serif", "serif", "monospace")]


class ToolWindow(QWidget):
    """One tool's window: frame, title bar, buttons, and the page in a
    scroll area (a window smaller than its page scrolls, as a pane does)."""

    def __init__(self, desk, tool_id, title, color):
        super().__init__(desk)
        self.desk, self.tool_id, self.title = desk, tool_id, title
        self.color = color
        self.active = False
        self.maximized = False
        self._drag = None           # (edges or "move", press point, start geometry)
        self._hover = None          # the title-bar button under the pointer
        self._pressed = None
        self._title_color = color
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        # A native window of its own, so Windows keeps the stacking order
        # (see the module docstring) - without dragging the desk along.
        self.setAttribute(Qt.WA_DontCreateNativeAncestors, True)
        self.setAttribute(Qt.WA_NativeWindow, True)
        self.scroller = QScrollArea(self)
        self.scroller.setWidgetResizable(True)
        self.scroller.setFrameShape(QFrame.NoFrame)
        # A QScrollArea is a QFrame, and the app stylesheet draws every
        # QFrame as a bordered card - the window already is one.
        self.scroller.setObjectName("deskWindowBody")
        self.scroller.setStyleSheet("#deskWindowBody { border: none; border-radius: 0px; }")
        self.hide()

    # ------------------------------------------------------------ page --
    def set_page(self, page):
        if self.scroller.widget() is not page:
            self.scroller.setWidget(page)
        page.show()

    def page(self):
        return self.scroller.widget()

    # --------------------------------------------------------- geometry --
    def frame_rect(self):
        """The window itself; the widget reaches SHADOW past its right and
        bottom edges for the shadow."""
        return QRect(0, 0, self.width() - SHADOW, self.height() - SHADOW)

    def content_rect(self):
        return self.frame_rect().adjusted(INSET, TITLE_H + BORDER, -INSET, -INSET)

    def resizeEvent(self, event):
        self.scroller.setGeometry(self.content_rect())
        self._clip_to_shape()
        super().resizeEvent(event)

    def _clip_to_shape(self):
        """A native window is a rectangle: clip it to the rounded frame and
        its shadow, so what's behind shows through the corners."""
        frame = QRectF(self.frame_rect())
        region = QRegion()
        for rect in (frame, frame.translated(SHADOW, SHADOW)):
            path = QPainterPath()
            path.addRoundedRect(rect.adjusted(-0.5, -0.5, 0.5, 0.5), RADIUS, RADIUS)
            region = region.united(QRegion(path.toFillPolygon().toPolygon()))
        self.setMask(region)

    def _buttons(self):
        """{"close"|"max"|"min": QRect}, right to left along the title bar."""
        f = self.frame_rect()
        y = f.top() + (TITLE_H - BTN) // 2 + 1
        out, x = {}, f.right() - 10 - BTN
        for name in ("close", "max", "min"):
            out[name] = QRect(x, y, BTN, BTN)
            x -= BTN + BTN_GAP
        return out

    def _edges(self, pos):
        """Edges a press here resizes (the shadow counts as the edge it's
        on); a title-bar button wins over a corner."""
        if self.maximized or self._button_at(pos):
            return frozenset()
        f = self.frame_rect()
        return dl.edges_at(pos.x(), pos.y(), (f.left(), f.top(), f.right(), f.bottom()), GRIP, CORNER)

    def _on_title(self, pos):
        return pos.y() <= self.frame_rect().top() + TITLE_H and self.frame_rect().contains(pos)

    def _button_at(self, pos):
        for name, rect in self._buttons().items():
            if rect.contains(pos):
                return name
        return None

    # ------------------------------------------------------------ mouse --
    def mousePressEvent(self, event):
        self.desk.activate(self.tool_id)
        if event.button() != Qt.LeftButton:
            return
        pos = event.position().toPoint()
        self._pressed = self._button_at(pos)
        if self._pressed:
            self.update()
            return
        edges = self._edges(pos)
        if edges:
            self._drag = (edges, event.globalPosition().toPoint(), self.geometry())
        elif self._on_title(pos) and not self.maximized:
            self._drag = ("move", event.globalPosition().toPoint(), self.geometry())

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        if self._drag:
            kind, start, geom = self._drag
            d = event.globalPosition().toPoint() - start
            g = (geom.x(), geom.y(), geom.width(), geom.height())
            if kind == "move":
                g = dl.clamp((g[0] + d.x(), g[1] + d.y(), g[2], g[3]), self.desk.area())
            else:
                g = dl.resize(g, kind, d.x(), d.y(), self.desk.area())
            self.setGeometry(QRect(*g))
            return
        hover = self._button_at(pos)
        if hover != self._hover:
            self._hover = hover
            self.update()
        edges = self._edges(pos)
        self.setCursor(_CURSORS.get(edges, Qt.ArrowCursor))

    def mouseReleaseEvent(self, event):
        pos = event.position().toPoint()
        if self._pressed:
            name, self._pressed = self._pressed, None
            self.update()
            if self._button_at(pos) == name:
                {"close": self.desk.close, "max": self.desk.toggle_max,
                 "min": self.desk.minimize}[name](self.tool_id)
            return
        if self._drag:
            self._drag = None
            g = self.geometry()
            self.desk.moved(self.tool_id, (g.x(), g.y(), g.width(), g.height()))

    def mouseDoubleClickEvent(self, event):
        pos = event.position().toPoint()
        if self._on_title(pos) and not self._button_at(pos):
            self.desk.toggle_max(self.tool_id)

    def leaveEvent(self, event):
        if self._hover:
            self._hover = None
            self.update()
        self.unsetCursor()
        super().leaveEvent(event)

    # ------------------------------------------------------------ paint --
    def paintEvent(self, _event):
        c = self.desk.palette_colors
        ink, paper = QColor(c["ink"]), QColor(c["surface"])
        f = QRectF(self.frame_rect()).adjusted(BORDER / 2, BORDER / 2, -BORDER / 2, -BORDER / 2)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        p.setPen(Qt.NoPen)
        p.setBrush(ink)
        p.drawRoundedRect(f.translated(SHADOW, SHADOW), RADIUS, RADIUS)

        body = QPainterPath()
        body.addRoundedRect(f, RADIUS, RADIUS)
        p.setBrush(paper)
        p.drawPath(body)

        bar = QPainterPath()
        bar.addRect(QRectF(f.left(), f.top(), f.width(), TITLE_H))
        title_color = self.color if self.active else _blend(self.color, c["surface"], 0.55)
        self._title_color = title_color
        p.setBrush(QColor(title_color))
        p.drawPath(body.intersected(bar))

        pen = QPen(ink, BORDER)
        p.setPen(pen)
        p.drawLine(QPointF(f.left(), f.top() + TITLE_H), QPointF(f.right(), f.top() + TITLE_H))
        p.setBrush(Qt.NoBrush)
        p.drawPath(body)

        font = QFont()
        font.setFamilies(self.desk.font_families)
        font.setPixelSize(13)
        font.setBold(True)
        p.setFont(font)
        buttons = self._buttons()
        text_rect = QRect(16, 0, buttons["min"].left() - 24, TITLE_H + 1)
        # Dark text on the candy bar; faded on an inactive one, but never
        # past readable (a dark palette fades the bar toward dark).
        title_text = c["title_text"] if self.active else ensure_contrast(
            _blend(c["title_text"], title_color, 0.35), title_color, 3.0)
        p.setPen(QColor(title_text))
        p.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                   p.fontMetrics().elidedText(self.title, Qt.ElideRight, text_rect.width()))

        for name, rect in buttons.items():
            self._paint_button(p, name, QRectF(rect), c)
        p.end()

    def _paint_button(self, p, name, r, c):
        ink = QColor(c["ink"])
        # A light tint of the bar itself, so the dark glyph reads on any palette.
        fill = c["close"] if name == "close" else _blend(self._title_color, "#FFFFFF", 0.6)
        if self._pressed == name:
            fill = _blend(fill, c["ink"], 0.25)
        elif self._hover == name:
            fill = _blend(fill, c["ink"], 0.1)
        p.setPen(QPen(ink, BORDER))
        p.setBrush(QColor(fill))
        p.drawRoundedRect(r.adjusted(1, 1, -1, -1), 5, 5)
        m = r.adjusted(6, 6, -6, -6)
        pen = QPen(QColor(c["title_text"]), 2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if name == "close":
            p.drawLine(m.topLeft(), m.bottomRight())
            p.drawLine(m.topRight(), m.bottomLeft())
        elif name == "min":
            p.drawLine(QPointF(m.left(), m.bottom()), QPointF(m.right(), m.bottom()))
        elif self.maximized:
            p.drawRect(m.adjusted(0, 2, -2, 0))
            p.drawLine(QPointF(m.left() + 2, m.top()), QPointF(m.right(), m.top()))
            p.drawLine(QPointF(m.right(), m.top()), QPointF(m.right(), m.bottom() - 2))
        else:
            p.drawRect(m)


class DesktopArea(QWidget):
    """The desk: every tool window, and the dotted surface behind them.

    `pages` maps tool_id -> ToolPage; `stash(page)` takes back a page whose
    window closes (the shell keeps it, running, out of sight). The shell
    hears about every change through `on_change(front_id)` - to redraw the
    rail and save - with the tool whose window is now in front (or None)."""

    def __init__(self, parent, pages, titles, stash, on_change):
        super().__init__(parent)
        self.pages, self.titles, self.stash = pages, titles, stash
        self.on_change = on_change
        self.state = dl.DesktopState()
        self.windows = {}
        self.palette_colors = {"ink": "#000000", "text": "#000000", "title_text": "#000000",
                               "surface": "#FFFFFF", "desk": "#EEEEEE", "close": "#F28B8B",
                               "windows": ["#CCCCCC"]}
        self.font_families = ["Segoe UI"]
        self._dots = None
        QApplication.instance().installEventFilter(self)
        self.hide()

    # ------------------------------------------------------------- theme --
    def set_theme(self, tokens, shape):
        c = desktop_colors(tokens)
        self.palette_colors = {
            "ink": c["ink"], "text": c["text"], "title_text": c["title_text"],
            "surface": tokens["surface"], "desk": c["desk"],
            "close": _blend(tokens["danger"], "#FFFFFF", 0.5),   # the pages' danger fill
            "windows": c["windows"],
        }
        self.font_families = _families(shape["font"]) or ["Segoe UI"]
        self._dots = None
        self.sync()
        self.update()

    # ------------------------------------------------------------- state --
    def area(self):
        return (max(1, self.width()), max(1, self.height()))

    def load(self, data):
        self.state = dl.DesktopState.from_dict(data, set(self.pages))

    def _window(self, tool_id):
        if tool_id not in self.windows:
            self.windows[tool_id] = ToolWindow(self, tool_id, self.titles.get(tool_id, tool_id), "#CCCCCC")
        return self.windows[tool_id]

    def window_color(self, tool_id):
        """The title-bar colour of a tool's window (the taskbar shows it)."""
        return self._color(tool_id)

    def _color(self, tool_id):
        colors = self.palette_colors["windows"]
        return colors[self.state.windows[tool_id]["color"] % len(colors)]

    def _preferred(self, tool_id):
        """A new window's size: wide enough for its page's minimum, and a
        good share of the desk."""
        area = self.area()
        need = self.pages[tool_id].minimumSizeHint().width() + 2 * INSET + SHADOW
        return (max(need, area[0] * 0.62), area[1] * 0.82)

    def _changed(self):
        self.on_change(self.state.front())

    # --------------------------------------------------------- the shell --
    def show_desk(self, focus_id=None):
        """Switch to the desktop: every window that was open comes back.
        With none open, `focus_id` (the tool the panes were showing) opens."""
        self.show()
        if not self.state.open_ids() and focus_id:
            self.open(focus_id)
        else:
            for tool_id in self.state.open_ids():
                w = self.state.windows[tool_id]
                if not w["min"]:
                    self._window(tool_id).set_page(self.pages[tool_id])
            self.sync()
            self._changed()

    def release_all(self):
        """Back to the panes: every page returns to the shell - moved
        straight there, then its window is thrown away (a scroll area whose
        page left it isn't fit to reuse). Where the windows were is kept for
        next time."""
        for win in self.windows.values():
            page = win.page()
            if page is not None:
                self.stash(page)
            win.hide()
            win.deleteLater()
        self.windows.clear()
        self.hide()

    def open(self, tool_id):
        """Open a tool's window (or bring it back from minimised, or to the
        front) - the rail's click."""
        if tool_id not in self.pages:
            return
        newly = self.state.open(tool_id)
        win = self._window(tool_id)
        page = self.pages[tool_id]
        win.set_page(page)
        self.sync()
        page.setFocus()
        if newly:
            page.on_shown()
        self._changed()

    def activate(self, tool_id):
        window = self.windows.get(tool_id)
        if window is None:
            return  # nothing to activate - its window hasn't been opened yet
        if self.state.front() != tool_id or not window.active:
            self.state.raise_(tool_id)
            self.sync()
            self._changed()

    def close(self, tool_id):
        self.state.close(tool_id)
        self._put_away(tool_id)

    def minimize(self, tool_id):
        self.state.minimize(tool_id)
        self._put_away(tool_id)

    def _put_away(self, tool_id):
        """Closed or minimised: the window hides with its page still in it."""
        win = self.windows.get(tool_id)
        if win is not None:
            win.hide()
        self.sync()
        self._changed()

    def toggle_max(self, tool_id):
        self.state.toggle_max(tool_id)
        self.sync()
        self._changed()

    def moved(self, tool_id, geom):
        """A drag or resize finished."""
        self.state.set_geom(tool_id, geom)
        self._changed()

    def cascade(self):
        self.state.cascade(self.area())
        self._restore_minimised()

    def tile(self):
        self.state.tile(self.area())
        self._restore_minimised()

    def _restore_minimised(self):
        for tool_id in self.state.open_ids():
            self._window(tool_id).set_page(self.pages[tool_id])
        self.sync()
        self._changed()

    def keep_only(self, visible_ids):
        for tool_id in self.state.open_ids():
            if tool_id not in visible_ids:
                self.close(tool_id)

    def window_state(self, tool_id):
        return self.state.window_state(tool_id)

    def front(self):
        return self.state.front()

    def sync(self):
        """Put every window where the state says, in stacking order."""
        area = self.area()
        front = self.state.front()
        for tool_id in self.state.order:
            w = self.state.windows[tool_id]
            win = self.windows.get(tool_id)
            if not w["open"] or w["min"]:
                if win is not None:
                    win.hide()
                continue
            if w["geom"] is None:
                if area[0] < dl.MIN_DESK[0] or area[1] < dl.MIN_DESK[1]:
                    continue    # placed once the desk has its size (resizeEvent)
                self.state.place_new(tool_id, area, self._preferred(tool_id))
            win = self._window(tool_id)
            win.color = self._color(tool_id)
            win.maximized = w["max"]
            geom = (0, 0, *area) if w["max"] else dl.clamp(w["geom"] or (0, 0, *area), area)
            win.setGeometry(QRect(*geom))
            win.active = tool_id == front
            win.show()
            win.raise_()
            win.update()

    # ------------------------------------------------------------ events --
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.sync()

    def eventFilter(self, obj, event):
        # A click inside a window's page (the web view's own widgets) brings
        # that window forward too - the page eats the press before the
        # frame could see it.
        if event.type() == QEvent.MouseButtonPress and self.isVisible() and isinstance(obj, QWidget):
            widget = obj
            while widget is not None and widget is not self:
                if isinstance(widget, ToolWindow) and widget.parentWidget() is self:
                    if widget.tool_id != self.state.front():
                        self.activate(widget.tool_id)
                    break
                widget = widget.parentWidget()
        return False

    def _dot_brush(self):
        """The desk's staggered dot grid as one tile, painted once per theme."""
        if self._dots is None:
            c = self.palette_colors
            tile = QPixmap(DOT_STEP * 2, DOT_STEP * 2)
            tile.fill(QColor(c["desk"]))
            # Dots in the text colour: dark on paper, light on a dark desk.
            dot = QColor(c["text"])
            dot.setAlpha(34)
            p = QPainter(tile)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(dot)
            half = DOT_STEP / 2
            for x, y in ((half, half), (half + DOT_STEP, half),
                         (DOT_STEP, half + DOT_STEP), (0, half + DOT_STEP), (DOT_STEP * 2, half + DOT_STEP)):
                p.drawEllipse(QPointF(x, y), 1.4, 1.4)
            p.end()
            self._dots = QBrush(tile)
        return self._dots

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(event.rect(), self._dot_brush())
        p.end()
