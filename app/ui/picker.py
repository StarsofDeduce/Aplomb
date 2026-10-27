"""窗口选择器 —— Alt+Tab 式的浮层。

缩略图按需逐张截取（每帧一张），界面先出来，图慢慢补上，
避免一次性阻塞 UI，也避免同时堆一批大位图。
关闭时立刻丢弃所有缩略图，托管内存回到基线。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QCursor,
    QGuiApplication,
    QIcon,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import thumbnails
from .. import win32api as W
from ..icons import window_icon
from ..windows import WindowInfo, list_windows
from . import theme

CARD_W, CARD_H = 198, 164
THUMB_W, THUMB_H = 182, 102
GAP = 12
COLS = 4
PAD = 24
SHADOW = 18
MAX_ROWS = 3


class SearchBox(QLineEdit):
    navigate = Signal(int, int)
    accepted = Signal()
    cancelled = Signal()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            dx = -1 if key == Qt.Key_Left else (1 if key == Qt.Key_Right else 0)
            dy = -1 if key == Qt.Key_Up else (1 if key == Qt.Key_Down else 0)
            self.navigate.emit(dx, dy)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self.accepted.emit()
            return
        if key == Qt.Key_Escape:
            self.cancelled.emit()
            return
        super().keyPressEvent(event)


class ThumbView(QWidget):
    """圆角裁切的缩略图，没有图就画一个居中的程序图标。"""

    def __init__(self, icon: QIcon, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(THUMB_W, THUMB_H)
        self._icon = icon
        self._pixmap: QPixmap | None = None

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self.update()

    def clear(self) -> None:
        self._pixmap = None

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, 7, 7)
        painter.setClipPath(path)

        tokens = theme.palette(self._mode)
        painter.fillRect(rect, QColor(tokens["control"]))

        if self._pixmap is not None and not self._pixmap.isNull():
            painter.drawPixmap(self.rect(), self._pixmap)
        else:
            side = 40
            pix = self._icon.pixmap(QSize(side, side))
            if not pix.isNull():
                painter.drawPixmap(
                    (self.width() - side) // 2,
                    (self.height() - side) // 2,
                    pix,
                )
        painter.end()

    _mode = "dark"

    def apply_theme(self, mode: str) -> None:
        self._mode = mode
        self.update()


class ThumbCard(QFrame):
    clicked = Signal(int, bool)  # hwnd, ctrl_pressed

    def __init__(self, info: WindowInfo, mode: str = "dark",
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.info = info
        self.setObjectName("ThumbCard")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedSize(CARD_W, CARD_H)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("selected", "false")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        icon = window_icon(info.exe_path)
        self.thumb = ThumbView(icon)
        self.thumb.apply_theme(mode)
        layout.addWidget(self.thumb)

        title = QLabel(self)
        title.setObjectName("CardTitle")
        title.setFixedHeight(19)
        title.setFixedWidth(THUMB_W)
        # 先让样式生效再量字体，否则拿到的是默认字号，省略算短了会溢出卡片
        title.ensurePolished()
        metrics = title.fontMetrics()
        title.setText(metrics.elidedText(info.title, Qt.ElideRight, THUMB_W - 4))
        layout.addWidget(title)

        subtitle = QLabel(info.exe_name or "未知程序", self)
        subtitle.setObjectName("CardSubtitle")
        subtitle.setFixedHeight(16)
        subtitle.setFixedWidth(THUMB_W)
        subtitle.ensurePolished()
        subtitle.setText(subtitle.fontMetrics().elidedText(
            info.exe_name or "未知程序", Qt.ElideRight, THUMB_W - 4))
        layout.addWidget(subtitle)

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", "true" if selected else "false")
        self.style().unpolish(self)
        self.style().polish(self)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self.clicked.emit(self.info.hwnd, bool(event.modifiers() & Qt.ControlModifier))
        super().mousePressEvent(event)


class PickerWindow(QWidget):
    chosen = Signal(list)   # list[int] hwnd
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Tool
            | Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.NoDropShadowWindowHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("Root")

        self._cards: list[ThumbCard] = []
        self._visible: list[ThumbCard] = []
        self._current = 0
        self._selected: list[int] = []
        self._thumb_queue: list[ThumbCard] = []
        self._mode = "dark"
        self._previous_hwnd = 0
        self._armed = False

        self._build_ui()

        self._thumb_timer = QTimer(self)
        self._thumb_timer.setInterval(8)
        self._thumb_timer.timeout.connect(self._capture_next)

        self._fade = None

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)

        self.body = QFrame(self)
        self.body.setObjectName("PickerBody")
        self.body.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(self.body)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(34)
        shadow.setOffset(0, 8)
        shadow.setColor(Qt.black)
        self.body.setGraphicsEffect(shadow)

        body_layout = QVBoxLayout(self.body)
        body_layout.setContentsMargins(PAD, 18, PAD, 18)
        body_layout.setSpacing(0)

        # 搜索行
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(9)

        self.search_icon = QLabel(self.body)
        self.search_icon.setFixedSize(16, 16)
        self.search_icon.setPixmap(self._magnifier())
        header.addWidget(self.search_icon, 0, Qt.AlignVCenter)

        self.search = SearchBox(self.body)
        self.search.setObjectName("SearchBox")
        self.search.setPlaceholderText("搜索窗口…")
        self.search.textChanged.connect(self._apply_filter)
        self.search.navigate.connect(self._navigate)
        self.search.accepted.connect(self._commit)
        self.search.cancelled.connect(self._cancel)
        header.addWidget(self.search, 1)

        self.count_label = QLabel(self.body)
        self.count_label.setObjectName("PickerHint")
        header.addWidget(self.count_label, 0, Qt.AlignVCenter)

        body_layout.addLayout(header)
        body_layout.addSpacing(12)

        # 网格
        self.scroll = QScrollArea(self.body)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setFocusPolicy(Qt.NoFocus)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.viewport().setAutoFillBackground(False)
        self.scroll.viewport().setFocusPolicy(Qt.NoFocus)

        self.grid_host = QWidget()
        self.grid_host.setAutoFillBackground(False)
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(GAP)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.grid_host)
        body_layout.addWidget(self.scroll)

        self.empty_label = QLabel("没有找到可居中的窗口", self.body)
        self.empty_label.setObjectName("PickerEmpty")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.hide()
        body_layout.addWidget(self.empty_label)

        body_layout.addSpacing(8)

        # 底部提示
        self.hint = QLabel("↑↓←→ 选择   ·   Enter 居中   ·   Ctrl+点击 多选   ·   Esc 取消", self.body)
        self.hint.setObjectName("PickerHint")
        body_layout.addWidget(self.hint)

    def _magnifier(self) -> QPixmap:
        tokens = theme.palette(self._mode)
        pix = QPixmap(32, 32)
        pix.setDevicePixelRatio(2.0)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = painter.pen()
        pen.setColor(QColor(tokens["text_faint"]))
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QRectF(2.6, 2.6, 8.6, 8.6))
        painter.drawLine(10.4, 10.4, 13.6, 13.6)
        painter.end()
        return pix

    # ------------------------------------------------------------------ #
    def open(self, ignore_list: list[str], show_thumbnails: bool = True) -> None:
        self._previous_hwnd = W.user32.GetForegroundWindow()
        self._selected.clear()
        self._current = 0
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)

        self._clear_cards()
        infos = list_windows(ignore_list, include_minimized=True, allow_own=True)
        for info in infos:
            card = ThumbCard(info, self._mode, self.grid_host)
            card.clicked.connect(self._on_card_clicked)
            self._cards.append(card)
            if show_thumbnails and not info.minimized:
                self._thumb_queue.append(card)

        self._visible = list(self._cards)
        self._relayout()

        if self._cards:
            self._set_current(0)
            self._selected = [self._cards[0].info.hwnd]
            self._cards[0].set_selected(True)

        self._resize_to_content(len(self._cards))
        self._place_window()
        self.show()
        self.raise_()
        W.force_foreground(int(self.winId()))
        self.search.setFocus(Qt.OtherFocusReason)

        # 刚显示时的焦点抖动不算"点了别处"
        self._armed = False
        QTimer.singleShot(220, self._arm)

        if self._thumb_queue:
            self._thumb_timer.start()

    def _arm(self) -> None:
        if self.isVisible():
            self._armed = True

    def _resize_to_content(self, count: int) -> None:
        rows = min(max(1, math.ceil(count / COLS)), MAX_ROWS) if count else 0
        content_w = CARD_W * COLS + GAP * (COLS - 1) + 14  # 14 给滚动条留位

        if count:
            grid_h = rows * CARD_H + (rows - 1) * GAP
            self.scroll.show()
            self.empty_label.hide()
        else:
            grid_h = CARD_H
            self.scroll.hide()
            self.empty_label.show()
            self.empty_label.setFixedHeight(CARD_H)

        self.scroll.setFixedWidth(content_w)
        self.scroll.setFixedHeight(grid_h)

        body_w = PAD * 2 + content_w
        self.body.setFixedWidth(body_w)
        layout = self.body.layout()
        if layout is not None:
            layout.activate()
        body_h = self.body.sizeHint().height()
        self.setFixedSize(body_w + SHADOW * 2, body_h + SHADOW * 2)

    def _place_window(self) -> None:
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = area.x() + (area.width() - self.width()) // 2
        y = area.y() + (area.height() - self.height()) // 2
        x = max(area.x() + 8, min(x, area.right() - self.width() - 8))
        y = max(area.y() + 8, min(y, area.bottom() - self.height() - 8))
        self.move(QPoint(x, y))

    # ------------------------------------------------------------------ #
    def _relayout(self) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self.grid_host)

        for index, card in enumerate(self._visible):
            card.show()
            self.grid.addWidget(card, index // COLS, index % COLS)

        for card in self._cards:
            if card not in self._visible:
                card.hide()

        self.count_label.setText(f"{len(self._visible)} 个窗口" if self._visible else "")
        self._resize_to_content(len(self._visible))

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()
        if query:
            tokens = [t for t in query.split() if t]
            self._visible = [
                c for c in self._cards
                if all(t in (c.info.title + " " + c.info.exe_name).lower() for t in tokens)
            ]
        else:
            self._visible = list(self._cards)

        self._current = 0
        self._relayout()
        if self._visible:
            self._set_current(0, keep_selection=True)
        self.scroll.verticalScrollBar().setValue(0)

    # ------------------------------------------------------------------ #
    def _set_current(self, index: int, keep_selection: bool = False) -> None:
        if not self._visible:
            return
        index = max(0, min(index, len(self._visible) - 1))
        self._current = index
        card = self._visible[index]
        if not keep_selection:
            for other in self._cards:
                other.set_selected(False)
            self._selected = [card.info.hwnd]
        card.set_selected(True)
        self.scroll.ensureWidgetVisible(card, 0, 12)
        self._update_hint()

    def _navigate(self, dx: int, dy: int) -> None:
        if not self._visible:
            return
        step = dx + dy * COLS
        self._set_current(self._current + step)

    def _on_card_clicked(self, hwnd: int, ctrl: bool) -> None:
        card = next((c for c in self._cards if c.info.hwnd == hwnd), None)
        if card is None:
            return

        if ctrl:
            if hwnd in self._selected:
                self._selected.remove(hwnd)
                card.set_selected(False)
            else:
                self._selected.append(hwnd)
                card.set_selected(True)
            if card in self._visible:
                self._current = self._visible.index(card)
        else:
            for other in self._cards:
                other.set_selected(False)
            self._selected = [hwnd]
            card.set_selected(True)
            if card in self._visible:
                self._current = self._visible.index(card)
        self._update_hint()
        # 点完卡片把焦点还给搜索框：否则焦点跑到滚动区上，
        # 后续按 Enter / 继续打字都不会有人接
        self.search.setFocus(Qt.MouseFocusReason)

    def _update_hint(self) -> None:
        if len(self._selected) > 1:
            self.hint.setText(f"已选 {len(self._selected)} 个窗口   ·   Enter 全部居中   ·   Esc 取消")
        else:
            self.hint.setText("↑↓←→ 选择   ·   Enter 居中   ·   Ctrl+点击 多选   ·   Esc 取消")

    # ------------------------------------------------------------------ #
    def _capture_next(self) -> None:
        while self._thumb_queue:
            card = self._thumb_queue.pop(0)
            image = thumbnails.capture(card.info.hwnd, THUMB_W * 2, THUMB_H * 2)
            if image is not None and not image.isNull():
                pixmap = QPixmap.fromImage(image)
                card.thumb.set_pixmap(
                    pixmap.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatioByExpanding,
                                  Qt.SmoothTransformation)
                )
            break
        if not self._thumb_queue:
            self._thumb_timer.stop()

    # ------------------------------------------------------------------ #
    def _commit(self) -> None:
        targets = list(self._selected)
        if not targets and self._visible:
            targets = [self._visible[self._current].info.hwnd]
        self.close_picker()
        if targets:
            self.chosen.emit(targets)

    def _cancel(self) -> None:
        previous = self._previous_hwnd
        self.close_picker()
        self.cancelled.emit()
        if previous and W.user32.IsWindow(previous):
            W.user32.SetForegroundWindow(previous)

    def close_picker(self) -> None:
        self._armed = False
        self._thumb_timer.stop()
        self._thumb_queue.clear()
        self.hide()
        # 立刻释放缩略图，别让它跟着常驻
        for card in self._cards:
            card.thumb.clear()
        self._clear_cards()

    def _clear_cards(self) -> None:
        for card in self._cards:
            card.thumb.clear()
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._visible.clear()
        self._selected.clear()
        self._thumb_queue.clear()
        # deleteLater 只排队，不立刻执行。这里强制把 DeferredDelete 事件投递掉，
        # 否则每开关一次浮层就会留下一批带缩略图的卡片，形成慢泄漏。
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)

    # ------------------------------------------------------------------ #
    def keyPressEvent(self, event) -> None:  # noqa: N802
        key = event.key()
        # 兜底：不管焦点落在哪个子控件上，回车/方向键都应该能用
        if key in (Qt.Key_Return, Qt.Key_Enter):
            self._commit()
            return
        if key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            dx = -1 if key == Qt.Key_Left else (1 if key == Qt.Key_Right else 0)
            dy = -1 if key == Qt.Key_Up else (1 if key == Qt.Key_Down else 0)
            self._navigate(dx, dy)
            return
        if key == Qt.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: N802
        # 点到别处就当作取消，避免浮层卡在屏幕上
        if self._armed and self.isVisible():
            self._cancel()
        super().focusOutEvent(event)

    def apply_theme(self, mode: str) -> None:
        self._mode = mode
        self.search_icon.setPixmap(self._magnifier())
        for card in self._cards:
            card.thumb.apply_theme(mode)
