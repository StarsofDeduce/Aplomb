"""开发用：界面冒烟测试。翻页、开关、改快捷键、切主题、选择器过滤与确认。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

# 冒烟测试会重置和修改配置，绝不能碰用户的真实设置。
_config_temp = tempfile.TemporaryDirectory(prefix="aplomb-ui-smoke-")
os.environ["APLOMB_CONFIG_DIR"] = _config_temp.name

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QCursor, QGuiApplication, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app import win32api as W
from app.config import Config
from app.ui import theme
from app.ui.picker import PickerWindow
from app.ui.settings_window import HOTKEY_FIELDS, SettingsWindow
from app.ui.window_chooser import WindowChooser

app = QApplication(sys.argv[:1])
app.setQuitOnLastWindowClosed(False)
app.setStyleSheet(theme.stylesheet("dark"))

ok = True


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok
    if not condition:
        ok = False
    print(f"[{'PASS' if condition else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


cfg = Config()
cfg.reset()

settings = SettingsWindow(cfg)
settings.apply_theme("dark")
settings.show()
app.processEvents()

# 标题栏两个控制键：尺寸一致、正方形
mini, close = settings.title_bar.min_button, settings.title_bar.close_button
check("控制键尺寸一致", mini.size() == close.size(), f"{mini.size()} vs {close.size()}")
check("控制键是正方形", mini.width() == mini.height(), f"{mini.width()}x{mini.height()}")
check("控制键垂直对齐", mini.y() == close.y(), f"{mini.y()} vs {close.y()}")

# 翻页
for index in range(4):
    settings.switch_page(index)
    app.processEvents()
    check(f"切到第 {index + 1} 页", settings.stack.currentIndex() == index)
    page = settings.stack.widget(index)
    minimum = page.layout().minimumSize().height()
    check(
        f"第 {index + 1} 页最小高度不超过内容区",
        minimum <= settings.stack.height(),
        f"{minimum} <= {settings.stack.height()}",
    )

# 连续拖动标题栏后窗口不能被布局拉伸，导航仍须可点击。
original_size = settings.size()
title = settings.title_bar
local = QPointF(120, 24)
start_global = QPointF(title.mapToGlobal(local.toPoint()))
QCoreApplication.sendEvent(title, QMouseEvent(
    QEvent.MouseButtonPress, local, start_global,
    Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
))
for step in range(1, 81):
    QCoreApplication.sendEvent(title, QMouseEvent(
        QEvent.MouseMove, local, start_global + QPointF(step * 3, step),
        Qt.NoButton, Qt.LeftButton, Qt.NoModifier,
    ))
    app.processEvents()
QCoreApplication.sendEvent(title, QMouseEvent(
    QEvent.MouseButtonRelease, local, start_global + QPointF(240, 80),
    Qt.LeftButton, Qt.NoButton, Qt.NoModifier,
))
app.processEvents()
check("连续拖动后设置窗口尺寸稳定", settings.size() == original_size,
      f"{settings.size()} vs {original_size}")
native_rect = W.get_window_rect(int(settings.winId()))
check(
    "连续拖动后 Win32 原生尺寸稳定",
    native_rect is not None
    and (native_rect.width, native_rect.height) == (original_size.width(), original_size.height()),
    "无窗口矩形" if native_rect is None else f"{native_rect.width}x{native_rect.height}",
)
QTest.mouseClick(settings._nav_group.button(1), Qt.LeftButton)
app.processEvents()
check("连续拖动后导航仍可点击", settings.stack.currentIndex() == 1)

# 开关
settings.switch_page(0)  # 常规页
app.processEvents()
settings.notify_toggle.setChecked(False)
app.processEvents()
check("关闭操作反馈写入配置", cfg.get("show_notifications") is False)
settings.notify_toggle.setChecked(True)
app.processEvents()
check("再打开", cfg.get("show_notifications") is True)

settings.shadow_toggle.setChecked(False)
app.processEvents()
check("关闭阴影修正写入配置", cfg.get("pixel_perfect") is False)
settings.shadow_toggle.setChecked(True)

# 主题切换
settings.theme_segment.set_value("light")
settings._on_theme("light")
app.setStyleSheet(theme.stylesheet("light"))
settings.apply_theme("light")
app.processEvents()
check("切到浅色", cfg.get("theme") == "light")
settings._on_theme("dark")
app.setStyleSheet(theme.stylesheet("dark"))
settings.apply_theme("dark")
app.processEvents()
check("切回深色", cfg.get("theme") == "dark")

# 忽略列表
cfg.set("ignore_list", ["foo.exe", "bar.exe"])
settings.refresh()
check("忽略列表载入", settings.ignore_list.count() == 2)
settings.ignore_list.item(0).setSelected(True)
settings._remove_ignore()
check("移除后剩 1 条", len(cfg.get("ignore_list", [])) == 1, str(cfg.get("ignore_list")))

# 精确自动居中规则、旧名单停用与双开关
cfg.set("remember_centered", True)
cfg.set("remember_after_manual_center", False)
cfg.set("remember_rules", [{
    "exe_path": r"C:\Program Files\Notes\note.exe",
    "exe_name": "note.exe",
    "class_name": "NotesMainWindow",
}])
settings.refresh()
check("界面只载入精确规则", settings.remember_list.count() == 1)
settings.remember_toggle.setChecked(False)
app.processEvents()
check("关闭自动居中写入配置", cfg.get("remember_centered") is False)
settings.remember_toggle.setChecked(True)
app.processEvents()
check("重新开启自动居中写入配置", cfg.get("remember_centered") is True)
settings.remember_after_manual_toggle.setChecked(True)
app.processEvents()
check("自动收录开关独立写入配置",
      cfg.get("remember_after_manual_center") is True)
settings.remember_list.item(0).setSelected(True)
settings._remove_remember()
check("精确规则可移除", cfg.get("remember_rules") == [])
check("规则名单空态提示可见", settings.remember_empty.isVisibleTo(settings.remember_list))

remember_chooser = WindowChooser([], settings, purpose="remember")
eligible_items = [
    remember_chooser.list.item(i)
    for i in range(remember_chooser.list.count())
    if remember_chooser.list.item(i).flags() != Qt.NoItemFlags
]
check("精确规则选择器可列出主窗口", len(eligible_items) > 0)
if eligible_items:
    eligible_items[0].setSelected(True)
    check("精确规则选择器返回完整窗口信息",
          len(remember_chooser.selected_windows()) == 1)
remember_chooser.close()

# 快捷键修改
settings.switch_page(1)  # 快捷键页
app.processEvents()
edit = settings._hotkey_edits["center_all"]
clear = settings._hotkey_clear_buttons["center_all"]
check("空快捷键的清空按钮初始禁用", not clear.isEnabled())

# 走与用户录制完成相同的 changed 信号路径，防止按钮状态只能靠重启刷新。
edit.changed.emit("Ctrl+Alt+K")
app.processEvents()
check("快捷键改后回显", edit.text().replace(" ", "") == "Ctrl+Alt+K", edit.text())
check("刚设置快捷键即可立即清空", clear.isEnabled())

clear.click()
app.processEvents()
check("点击清空按钮写入空快捷键", cfg.get("hotkey_center_all") == "")
check("清空后显示未设置", edit.text() == "未设置", edit.text())
check("清空后按钮禁用", not clear.isEnabled())

cfg.reset_hotkeys()
settings.refresh()
check("恢复默认值为空", all(cfg.get(key) == "" for _, key, _, _ in HOTKEY_FIELDS))

# 界面使用“禁止缩略图”的正向开关，但继续兼容原 picker_thumbnails 配置。
settings.thumb_toggle.setChecked(True)
app.processEvents()
check("开启不显示缩略图会关闭旧配置项",
      cfg.get("picker_thumbnails") is False)
settings.thumb_toggle.setChecked(False)
app.processEvents()
check("关闭不显示缩略图会恢复缩略图",
      cfg.get("picker_thumbnails") is True)

settings.hide()
app.processEvents()

# 选择器
picker = PickerWindow()
picker.apply_theme("dark")
received: list[list[int]] = []
picker.chosen.connect(lambda hwnds: received.append(hwnds))
picker.open(cfg.get("ignore_list", []), True)
app.processEvents()
total = len(picker._cards)
check("选择器载入窗口", total > 0, f"{total} 个")

picker_screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
picker_area = picker_screen.availableGeometry()
picker_center = picker.frameGeometry().center()
area_center = picker_area.center()
check(
    "多窗口选择器位于鼠标所在屏幕正中",
    abs(picker_center.x() - area_center.x()) <= 1
    and abs(picker_center.y() - area_center.y()) <= 1,
    f"picker=({picker_center.x()},{picker_center.y()}), "
    f"screen=({area_center.x()},{area_center.y()}), windows={total}",
)

picker.search.setText("zzzzzz-no-such-window")
app.processEvents()
check("过滤后为空", len(picker._visible) == 0)

picker.search.clear()
app.processEvents()
check("清空过滤后恢复", len(picker._visible) == total)

picker._navigate(1, 0)
app.processEvents()
check("方向键移动选中", picker._current == 1)

picker._commit()
app.processEvents()
check("确认后发出选中", len(received) == 1 and len(received[0]) == 1)
check("确认后浮层关闭", not picker.isVisible())
check("确认后卡片释放", len(picker._cards) == 0)

# 点选卡片之后按回车（用户报过的问题：点了卡片回车没反应）
picker.open(cfg.get("ignore_list", []), False)
app.processEvents()
check("重新打开后卡片就绪", len(picker._cards) >= 2)
if len(picker._cards) >= 2:
    target_hwnd = picker._cards[1].info.hwnd
    picker._on_card_clicked(target_hwnd, False)
    app.processEvents()
    check("点选卡片后焦点回到搜索框", picker.search.hasFocus())

    received.clear()
    QTest.keyClick(picker.search, Qt.Key_Return)
    app.processEvents()
    check("点选后按回车能确认", received == [[target_hwnd]], str(received))

    # 焦点不在搜索框时（比如点了空白处），回车同样要能用
    picker.open(cfg.get("ignore_list", []), False)
    app.processEvents()
    picker.search.clearFocus()
    app.processEvents()
    received.clear()
    QTest.keyClick(picker, Qt.Key_Return)
    app.processEvents()
    check("焦点不在搜索框时回车也能确认", len(received) == 1, str(received))

cfg.reset()
print("\n结果:", "全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)
