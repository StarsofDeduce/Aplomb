"""Aplomb · 从容 —— 程序入口。

常驻托盘，只注册三个全局热键，空闲时不跑任何定时器。
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# 管理员 broker 不创建 Qt、托盘或单实例对象，既减小权限面也降低驻留内存。
if __name__ == "__main__" and "--elevated-broker" in sys.argv[1:]:
    from app import elevated as _broker_elevated
    from app import win32api as _broker_win32api

    _broker_win32api.set_process_dpi_awareness()
    raise SystemExit(_broker_elevated.run_broker(sys.argv[1:]))

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon, QWidget

from app import APP_NAME
from app import autostart
from app import elevated
from app import win32api as W
from app import windows as winlib
from app.config import Config, system_prefers_dark
from app.hotkeys import HotkeyManager
from app.remember import RememberManager
from app.ui.picker import PickerWindow
from app.ui.settings_window import SettingsWindow
from app.ui.theme import NoFocusRectStyle, stylesheet
from app.ui.tray import TrayManager

_INSTANCE_ID = os.environ.get("APLOMB_TEST_INSTANCE_ID", "v1")
MUTEX_NAME = f"Global\\Aplomb_SingleInstance_{_INSTANCE_ID}"
INSTANCE_WINDOW_TITLE = f"Aplomb::SingleInstance::{_INSTANCE_ID}"
WM_APLOMB_SHOW_SETTINGS = 0x8000 + 17  # WM_APP + 17

HOTKEY_BINDINGS = (
    ("center_current", "hotkey_center_current"),
    ("center_select", "hotkey_center_select"),
    ("center_all", "hotkey_center_all"),
)


def acquire_single_instance():
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def ping_running_instance() -> bool:
    """找到已有实例的宿主窗口并让它弹出设置。

    不能用 FindWindowW 精确匹配标题 —— Qt 会在窗口标题后面追加
    " - <应用显示名>"，这里按前缀匹配才稳。
    """
    target = []

    def callback(hwnd, _):
        if W.get_window_text(hwnd).startswith(INSTANCE_WINDOW_TITLE):
            target.append(hwnd)
            return False
        return True

    W.user32.EnumWindows(W.WNDENUMPROC(callback), 0)
    if not target:
        return False
    W.user32.PostMessageW(target[0], WM_APLOMB_SHOW_SETTINGS, 0, 0)
    return True


class Aplomb(QObject, QAbstractNativeEventFilter):
    def __init__(self, qapp: QApplication, config: Config) -> None:
        QObject.__init__(self)
        QAbstractNativeEventFilter.__init__(self)
        self.qapp = qapp
        self.config = config
        self.mode = "dark"

        # 一个永不显示的宿主窗口：全局热键和"再开一个实例"的消息都投到这里
        self.host = QWidget()
        self.host.setWindowTitle(INSTANCE_WINDOW_TITLE)
        self.host.setWindowFlags(Qt.Tool)
        self.hwnd = int(self.host.winId())
        self.elevated_broker = elevated.BrokerClient(self.hwnd)
        qapp.aboutToQuit.connect(self.elevated_broker.close)

        self.hotkeys = HotkeyManager(self.hwnd, self)
        self.hotkeys.triggered.connect(self._on_hotkey)

        # 精确自动居中：匹配的主窗口出现时尽早处理一次，不持续争抢位置。
        self.remember = RememberManager(self.config, self)

        self.tray = TrayManager(self)
        self.settings: SettingsWindow | None = None
        self.picker: PickerWindow | None = None
        self._hotkey_failures: list[str] = []
        self._last_foreground = int(W.user32.GetForegroundWindow() or 0)
        self._foreground_proc = W.WINEVENTPROC(self._on_foreground_event)
        self._foreground_hook = W.user32.SetWinEventHook(
            W.EVENT_SYSTEM_FOREGROUND,
            W.EVENT_SYSTEM_FOREGROUND,
            None,
            self._foreground_proc,
            0,
            0,
            W.WINEVENT_OUTOFCONTEXT | W.WINEVENT_SKIPOWNPROCESS,
        )

        self._wire()

        qapp.installNativeEventFilter(self.hotkeys)
        qapp.installNativeEventFilter(self)

        self._apply_theme()
        self.reload_hotkeys()

    # ------------------------------------------------------------------ #
    def _wire(self) -> None:
        self.tray.center_current.connect(self.action_center_current)
        self.tray.center_select.connect(self.action_center_select)
        self.tray.center_all.connect(self.action_center_all)
        self.tray.show_settings.connect(self.show_settings)
        self.tray.quit_app.connect(self.quit)
        self.tray.autostart_toggled.connect(self._on_tray_autostart)

    def _ensure_settings(self) -> SettingsWindow:
        if self.settings is None:
            self.settings = SettingsWindow(self.config)
            self.settings.hotkeys_changed.connect(self._on_hotkeys_changed)
            self.settings.theme_changed.connect(self._on_theme_changed)
            self.settings.remember_changed.connect(self._on_remember_changed)
            self.settings.quit_requested.connect(self.quit)
            # 设置窗口开着时，新收录的精确规则实时出现。
            self.remember.rules_changed.connect(self._on_remembered_rules_changed)
            self.settings.apply_theme(self.mode)
            self.settings.notify_hotkey_result(self._hotkey_failures)
        return self.settings

    def _ensure_picker(self) -> PickerWindow:
        if self.picker is None:
            self.picker = PickerWindow()
            self.picker.chosen.connect(self._on_picker_chosen)
            self.picker.apply_theme(self.mode)
        return self.picker

    # ------------------------------------------------------------------ #
    def reload_hotkeys(self) -> list[str]:
        self.hotkeys.unregister_all()
        failed: list[str] = []
        for action, key in HOTKEY_BINDINGS:
            spec = self.config.get(key, "")
            if spec and not self.hotkeys.register(action, spec):
                failed.append(action)

        self.tray.update_labels(
            self.config.get("hotkey_center_current", ""),
            self.config.get("hotkey_center_select", ""),
            self.config.get("hotkey_center_all", ""),
        )
        self._hotkey_failures = failed
        if self.settings is not None:
            self.settings.notify_hotkey_result(failed)
        return failed

    def _on_hotkeys_changed(self) -> None:
        self.reload_hotkeys()

    def _on_remember_changed(self) -> None:
        self.remember.sync()

    def _on_remembered_rules_changed(self) -> None:
        if self.settings is not None:
            self.settings.refresh()

    def _on_tray_autostart(self, enabled: bool) -> None:
        if autostart.set_enabled(enabled):
            self.config.set("autostart", enabled)
        else:
            self.tray.set_autostart(not enabled)

    # ------------------------------------------------------------------ #
    def _apply_theme(self) -> None:
        choice = self.config.get("theme", "system")
        if choice in ("dark", "light"):
            self.mode = choice
        else:
            self.mode = "dark" if system_prefers_dark() else "light"
        self.qapp.setStyleSheet(stylesheet(self.mode))
        if self.settings is not None:
            self.settings.apply_theme(self.mode)
        if self.picker is not None:
            self.picker.apply_theme(self.mode)

    def _on_theme_changed(self, _mode: str) -> None:
        self._apply_theme()

    # ------------------------------------------------------------------ #
    def _notify(self, message: str) -> None:
        if self.config.get("show_notifications", True):
            self.tray.notify(APP_NAME, message)

    def _on_foreground_event(self, _hook, _event, hwnd, _id_object,
                             _id_child, _thread, _time_ms) -> None:
        """保存最近一个真实应用窗口，供托盘菜单动作恢复目标。"""
        try:
            hwnd = int(hwnd or 0)
            if hwnd and winlib.inspect(hwnd, allow_own=False) is not None:
                self._last_foreground = hwnd
        except Exception:
            pass

    def _elevated_center(self, infos: list[winlib.WindowInfo], *,
                         remember: bool) -> tuple[int, str]:
        """一次 UAC 授权处理一组被完整性级别拦截的窗口。"""
        if not infos:
            return 0, ""
        result = self.elevated_broker.center(
            [info.hwnd for info in infos],
            self.config.get("pixel_perfect", True),
        )
        if result.cancelled:
            return 0, "已取消管理员授权"
        if result.error:
            return 0, "管理员辅助进程未能完成移动"
        if remember and result.moved == len(infos):
            for info in infos:
                self.remember.record(info)
        if result.moved < len(infos):
            return result.moved, f"有 {len(infos) - result.moved} 个窗口仍未能居中"
        return result.moved, ""

    def _on_hotkey(self, action: str) -> None:
        if action == "center_current":
            self.action_center_current()
        elif action == "center_select":
            self.action_center_select()
        elif action == "center_all":
            self.action_center_all()

    def action_center_current(self) -> None:
        info, reason = winlib.foreground_info(self._last_foreground)
        if info is None:
            if reason:
                self._notify(reason)
            return
        ok, reason = winlib.center(
            info,
            self.config.get("pixel_perfect", True),
            on_centered=self.remember.record,
        )
        if not ok and reason == winlib.ELEVATION_REQUIRED:
            moved, reason = self._elevated_center([info], remember=True)
            ok = moved == 1
        # 没有原因说明是"无需提示"的情况，别弹空通知
        if not ok and reason:
            self._notify(reason)

    def action_center_all(self) -> None:
        elevated_reason = ""

        def center_elevated(infos: list[winlib.WindowInfo]) -> int:
            nonlocal elevated_reason
            moved, elevated_reason = self._elevated_center(infos, remember=False)
            return moved

        count, total = winlib.center_all(
            self.config.get("pixel_perfect", True),
            self.config.get("ignore_list", []),
            on_access_denied=center_elevated,
        )
        if count:
            message = f"已居中 {count} 个窗口"
            if elevated_reason:
                message += f"；{elevated_reason}"
            self._notify(message)
        elif elevated_reason:
            self._notify(elevated_reason)
        elif total == 0:
            self._notify("没有可居中的窗口")

    def action_center_select(self) -> None:
        picker = self._ensure_picker()
        if picker.isVisible():
            picker.close_picker()
            return
        picker.open(
            self.config.get("ignore_list", []),
            self.config.get("picker_thumbnails", True),
        )

    def _on_picker_chosen(self, hwnds: list[int]) -> None:
        pixel_perfect = self.config.get("pixel_perfect", True)
        done = 0
        denied: list[winlib.WindowInfo] = []
        for hwnd in hwnds:
            info = winlib.inspect(hwnd, allow_own=True)
            if info is None:
                continue
            ok, reason = winlib.center(info, pixel_perfect)
            if ok:
                done += 1
                self.remember.record(info)
            elif reason == winlib.ELEVATION_REQUIRED:
                denied.append(info)
        elevated_reason = ""
        if denied:
            moved, elevated_reason = self._elevated_center(denied, remember=True)
            done += moved
        if done:
            winlib.activate(hwnds[0])
            if elevated_reason:
                self._notify(f"已居中 {done} 个窗口；{elevated_reason}")
        else:
            self._notify(elevated_reason or "这些窗口没能居中")

    # ------------------------------------------------------------------ #
    def show_settings(self) -> None:
        window = self._ensure_settings()
        window.show()
        window.raise_()
        W.force_foreground(int(window.winId()))

    def quit(self) -> None:
        self.hotkeys.unregister_all()
        self.elevated_broker.close()
        if self.picker is not None:
            self.picker.close_picker()
        if self._foreground_hook:
            W.user32.UnhookWinEvent(self._foreground_hook)
            self._foreground_hook = None
        self.tray.hide()
        self.host.close()
        self.qapp.quit()

    # ------------------------------------------------------------------ #
    def nativeEventFilter(self, event_type, message):
        try:
            if bytes(event_type) in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
                msg = ctypes.cast(int(message), ctypes.POINTER(W.MSG)).contents
                if msg.message == WM_APLOMB_SHOW_SETTINGS:
                    # 不要在原生消息回调里直接建窗口、抢前台 —— 那是消息分发过程中，
                    # 一旦内部再触发消息循环就容易卡死。延到下一拍再处理。
                    QTimer.singleShot(0, self.show_settings)
        except Exception:
            pass
        return False, 0


def main() -> int:
    # 必须在 QApplication 之前：显式申报 DPI 感知，全程统一用物理像素坐标
    W.set_process_dpi_awareness()

    args = sys.argv[1:]

    app = QApplication(sys.argv)
    # 保留实际焦点和键盘操作，只去掉 Windows 样式绘制的虚线焦点框。
    app.setStyle(NoFocusRectStyle())
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    # 不设 applicationDisplayName：Qt 会把它追加到每个窗口标题后面，
    # 任务栏就会出现 "Aplomb - Aplomb" 这种重复。

    # 刻意不调用 app.setFont()：显式指定字体会让 Qt 为每个控件单独解析
    # 字体回退链，实测多占 50MB+ 内存。Qt 的默认字体已经能正确渲染中文。

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, APP_NAME, "系统托盘不可用，Aplomb 无法驻留。")
        return 1

    if acquire_single_instance() is None:
        if not ping_running_instance():
            QMessageBox.information(
                None, APP_NAME, f"{APP_NAME} 已经在后台运行，请在托盘图标上查看。"
            )
        return 0

    config = Config()
    aplomb = Aplomb(app, config)
    aplomb.tray.set_autostart(autostart.is_enabled())
    aplomb.tray.show()

    if "--settings" in args or config.first_run:
        aplomb.show_settings()

    exit_code = app.exec()
    del aplomb
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
