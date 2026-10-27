"""从当前打开的窗口里选择程序或精确窗口规则。"""

from __future__ import annotations

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..icons import window_icon
from ..windows import WindowInfo, automation_eligible, list_windows, normalized_exe_path
from .widgets import ChromeButton

SHADOW = 16


class WindowChooser(QDialog):
    def __init__(self, ignore_list: list[str] | None = None,
                 parent: QWidget | None = None, *, purpose: str = "ignore",
                 existing_rule_keys: set[tuple[str, str]] | None = None) -> None:
        super().__init__(parent)
        self._drag_offset: QPoint | None = None
        self._mode = "dark"
        self._purpose = purpose
        self._existing_rule_keys = existing_rule_keys or set()

        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setWindowTitle("选择自动居中的窗口" if purpose == "remember"
                            else "选择要忽略的窗口")
        self.setModal(True)
        self.setFixedSize(600, 540)

        self._build(ignore_list or [])
        self._center_on_parent(parent)

    # ------------------------------------------------------------------ #
    def _build(self, ignore_list: list[str]) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW, SHADOW, SHADOW)

        body = QFrame(self)
        body.setObjectName("WindowBody")
        body.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(body)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(32)
        shadow.setOffset(0, 8)
        shadow.setColor(Qt.black)
        body.setGraphicsEffect(shadow)

        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layout.addWidget(self._build_header(body))

        content = QVBoxLayout()
        content.setContentsMargins(24, 4, 24, 20)
        content.setSpacing(0)

        hint_text = (
            "只为选中的主窗口结构建立精确规则；同类名的功能窗、通知和对话框不会跟随。"
            if self._purpose == "remember"
            else "选中后，这些程序不会被任何居中动作移动，也不会出现在选择器里。"
        )
        hint = QLabel(hint_text, body)
        hint.setObjectName("RowDesc")
        hint.setWordWrap(True)
        content.addWidget(hint)
        content.addSpacing(12)

        frame = QFrame(body)
        frame.setObjectName("ListFrame")
        frame.setAttribute(Qt.WA_StyledBackground, True)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)

        self.list = QListWidget(frame)
        self.list.setIconSize(QSize(24, 24))
        self.list.setSelectionMode(QListWidget.ExtendedSelection)
        self.list.setTextElideMode(Qt.ElideRight)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setWordWrap(False)
        self.list.itemDoubleClicked.connect(lambda _: self.accept())
        frame_layout.addWidget(self.list)
        content.addWidget(frame, 1)

        layout.addLayout(content, 1)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(24, 0, 24, 20)
        buttons.setSpacing(8)

        self.count_label = QLabel("", body)
        self.count_label.setObjectName("RowDesc")
        buttons.addWidget(self.count_label, 1)

        cancel = QPushButton("取消", body)
        cancel.setObjectName("GhostButton")
        cancel.setCursor(Qt.PointingHandCursor)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        confirm = QPushButton("添加选中", body)
        confirm.setObjectName("PrimaryButton")
        confirm.setCursor(Qt.PointingHandCursor)
        confirm.clicked.connect(self.accept)
        buttons.addWidget(confirm)

        layout.addLayout(buttons)

        self.list.itemSelectionChanged.connect(self._update_count)
        self._fill(ignore_list)

    def _build_header(self, parent: QWidget) -> QWidget:
        header = QWidget(parent)
        header.setFixedHeight(54)
        layout = QHBoxLayout(header)
        layout.setContentsMargins(24, 0, 14, 0)
        layout.setSpacing(10)

        title = QLabel(
            "选择自动居中的窗口" if self._purpose == "remember" else "选择要忽略的窗口",
            header,
        )
        title.setObjectName("AppName")
        layout.addWidget(title)

        layout.addStretch(1)

        close = ChromeButton("close", header)
        close.clicked.connect(self.reject)
        layout.addWidget(close)
        self._close_button = close
        return header

    def _fill(self, ignore_list: list[str]) -> None:
        existing = {name.lower() for name in ignore_list}
        for info in list_windows([], include_minimized=True):
            if info.exe_name.lower() in existing:
                continue
            if self._purpose == "remember":
                key = (normalized_exe_path(info.exe_path), info.class_name.casefold())
                if not automation_eligible(info) or key in self._existing_rule_keys:
                    continue
            item = QListWidgetItem(f"{info.title}   ·   {info.exe_name}")
            item.setIcon(window_icon(info.exe_path))
            item.setData(Qt.UserRole, info)
            detail = f"{info.title}\n{info.exe_path}"
            if self._purpose == "remember":
                detail += f"\n窗口类：{info.class_name}"
            item.setToolTip(detail)
            self.list.addItem(item)

        if self.list.count() == 0:
            placeholder = QListWidgetItem("没有找到可添加的窗口")
            placeholder.setFlags(Qt.NoItemFlags)
            self.list.addItem(placeholder)
        self._update_count()

    def _update_count(self) -> None:
        count = len(self.list.selectedItems())
        self.count_label.setText(f"已选 {count} 个" if count else "可按住 Ctrl 多选")

    # ------------------------------------------------------------------ #
    def selected_exe_names(self) -> list[str]:
        names: list[str] = []
        for item in self.list.selectedItems():
            info = item.data(Qt.UserRole)
            name = info.exe_name if isinstance(info, WindowInfo) else ""
            if name and name not in names:
                names.append(name)
        return names

    def selected_windows(self) -> list[WindowInfo]:
        result: list[WindowInfo] = []
        for item in self.list.selectedItems():
            info = item.data(Qt.UserRole)
            if isinstance(info, WindowInfo):
                result.append(info)
        return result

    def _center_on_parent(self, parent: QWidget | None) -> None:
        screen = QGuiApplication.screenAt(QGuiApplication.primaryScreen().geometry().center())
        area = (parent.screen() if parent is not None else screen).availableGeometry()
        if parent is not None:
            center = parent.frameGeometry().center()
        else:
            center = area.center()
        self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)

    def apply_theme(self, mode: str) -> None:
        self._mode = mode
        self._close_button.apply_theme(mode)

    # 拖动
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and event.position().y() < SHADOW + 54:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)
