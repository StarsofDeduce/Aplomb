"""窗口盘点与居中算法。

居中的关键不是把 GetWindowRect 摆中间 —— 那个矩形包含 DWM 画的不可见阴影，
照它居中在视觉上会偏。这里用 DWMWA_EXTENDED_FRAME_BOUNDS 拿到真实可见边框，
再把它对齐到工作区正中，最后把阴影偏移补回去。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes
from dataclasses import dataclass

from . import win32api as W

# 这些是外壳窗口，不属于"应用窗口"
SHELL_CLASSES = frozenset({
    "Progman",
    "WorkerW",
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "MultitaskingViewFrame",
    "ForegroundStaging",
    "Windows.UI.Core.CoreWindow",
    "ApplicationManager_DesktopShellWindow",
    "XamlExplorerHostIslandWindow",
    "Windows.Internal.Shell.TabProxyWindow",
    "TaskListThumbnailWnd",
    "EdgeUiInputTopWndClass",
    "NarratorHelperWindow",
})

# 系统级不可移动的窗口
IMMOVABLE_CLASSES = frozenset({
    "Shell_TrayWnd",
    "Shell_SecondaryTrayWnd",
    "Progman",
    "WorkerW",
})

ELEVATION_REQUIRED = "目标窗口需要管理员授权"


@dataclass(slots=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    exe_path: str
    exe_name: str
    rect: W.RECT
    monitor_rect: W.RECT
    work_rect: W.RECT
    minimized: bool
    maximized: bool
    fullscreen: bool
    style: int
    ex_style: int
    owner: int

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.title}"


def _own_pid() -> int:
    return ctypes.windll.kernel32.GetCurrentProcessId()


def _own_exe() -> str:
    """打包后指向 Aplomb.exe 本身，开发时为空（不能把所有 python.exe 都排掉）。"""
    return sys.executable.lower() if getattr(sys, "frozen", False) else ""


def _exe_name(path: str) -> str:
    return path.rsplit("\\", 1)[-1] if path else ""


# 阴影/隐形边框的单侧偏移不会超过这个值。超过说明两套坐标系混了
# （典型：DPI 虚拟化下 GetWindowRect 与 DWM 边框不在一个尺度），
# 此时必须放弃"像素级修正"，退回普通矩形居中，宁可不准也不能乱飞。
MAX_SHADOW_OFFSET = 32


def _work_to_screen(rect: W.RECT, monitor: W.MONITORINFO) -> W.RECT:
    """WINDOWPLACEMENT 的工作区坐标 -> 屏幕坐标。"""
    dx = monitor.rcMonitor.left - monitor.rcWork.left
    dy = monitor.rcMonitor.top - monitor.rcWork.top
    return W.RECT(rect.left + dx, rect.top + dy, rect.right + dx, rect.bottom + dy)


def _screen_to_work(rect: W.RECT, monitor: W.MONITORINFO) -> W.RECT:
    dx = monitor.rcMonitor.left - monitor.rcWork.left
    dy = monitor.rcMonitor.top - monitor.rcWork.top
    return W.RECT(rect.left - dx, rect.top - dy, rect.right - dx, rect.bottom - dy)


def _monitor_for_rect(rect: W.RECT) -> W.MONITORINFO | None:
    """矩形中心所在的显示器。"""
    pt = W.POINT((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    monitor = W.user32.MonitorFromPoint(pt, W.MONITOR_DEFAULTTONEAREST)
    if not monitor:
        return None
    info = W.MONITORINFO()
    info.cbSize = ctypes.sizeof(W.MONITORINFO)
    if not W.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    return info


def _restore_geometry(hwnd: int) -> tuple[W.RECT, W.MONITORINFO] | None:
    """最小化或最大化窗口的还原矩形 + 归属显示器。

    归属按还原位置判断 —— 停车位 (-32000,-32000) 在多显示器时会指到
    错误的屏上，用它的工作区算居中目标就跟着错。
    """
    placement = W.get_placement(hwnd)
    if placement is None:
        return None
    normal = placement.rcNormalPosition
    if normal.width <= 0 or normal.height <= 0:
        return None
    monitor = _monitor_for_rect(normal) or W.get_monitor_info(hwnd)
    if monitor is None:
        return None
    return _work_to_screen(normal, monitor), monitor


def _effective_rect(hwnd: int, fallback: W.RECT | None = None) -> W.RECT | None:
    """窗口的"真实"矩形。

    最小化窗口的 GetWindowRect 是 (-32000,-32000) 的停车位，宽高也是
    任务栏按钮般的残影尺寸 —— 拿它算几何会把还原矩形写坏。最小化时
    一律改用 WINDOWPLACEMENT 的还原矩形。
    """
    if W.user32.IsIconic(hwnd):
        geometry = _restore_geometry(hwnd)
        if geometry is not None:
            return geometry[0]
    return W.get_window_rect(hwnd) or fallback


def _is_fullscreen(frame: W.RECT, monitor: W.RECT, style: int) -> bool:
    if not (style & W.WS_POPUP):
        return False
    return (
        abs(frame.left - monitor.left) <= 2
        and abs(frame.top - monitor.top) <= 2
        and abs(frame.right - monitor.right) <= 2
        and abs(frame.bottom - monitor.bottom) <= 2
    )


def inspect(hwnd: int, allow_own: bool = False, *,
            require_visible: bool = True,
            require_title: bool = True) -> WindowInfo | None:
    """把 hwnd 变成 WindowInfo，不合格就返回 None。

    allow_own=True 时不排除本程序自己的窗口 —— 用于「用户明确要居中当前窗口」
    这一种情况（焦点就在我们自己的设置界面上）。
    """
    if not hwnd or not W.user32.IsWindow(hwnd):
        return None

    style = W.GetWindowLongPtrW(hwnd, W.GWL_STYLE)
    if require_visible and not (style & W.WS_VISIBLE):
        return None
    if W.is_cloaked(hwnd):
        return None

    class_name = W.get_class_name(hwnd)
    if class_name in SHELL_CLASSES:
        return None

    ex_style = W.GetWindowLongPtrW(hwnd, W.GWL_EXSTYLE)
    if ex_style & W.WS_EX_TOOLWINDOW and not (ex_style & W.WS_EX_APPWINDOW):
        return None

    # 先判断是否卡死，再去读标题 —— 顺序不能反，读标题会跟目标进程通信
    if W.user32.IsHungAppWindow(hwnd):
        return None

    title = W.get_window_text(hwnd) if require_title else ""
    if require_title and not title:
        return None

    pid = wintypes.DWORD(0)
    W.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == _own_pid() and not allow_own:
        return None

    monitor = None
    rect = None
    if W.user32.IsIconic(hwnd):
        geometry = _restore_geometry(hwnd)
        if geometry is not None:
            rect, monitor = geometry
    if rect is None:
        rect = W.get_window_rect(hwnd)
        monitor = W.get_monitor_info(hwnd)
    if rect is None or monitor is None or rect.width <= 0 or rect.height <= 0:
        return None

    frame = W.get_visible_frame(hwnd) or rect
    path = W.get_process_path(pid.value)

    # 本进程，或者另一个 Aplomb 实例 —— 批量操作和列表里都不该出现
    own_exe = _own_exe()
    if not allow_own and own_exe and path and path.lower() == own_exe:
        return None

    return WindowInfo(
        hwnd=hwnd,
        title=title,
        class_name=class_name,
        pid=pid.value,
        exe_path=path,
        exe_name=_exe_name(path),
        rect=rect,
        monitor_rect=monitor.rcMonitor,
        work_rect=monitor.rcWork,
        minimized=bool(W.user32.IsIconic(hwnd)),
        maximized=bool(W.user32.IsZoomed(hwnd)),
        fullscreen=_is_fullscreen(frame, monitor.rcMonitor, style),
        style=style,
        ex_style=ex_style,
        owner=int(W.user32.GetWindow(hwnd, W.GW_OWNER) or 0),
    )


def normalized_exe_path(path: str) -> str:
    """用于精确规则匹配的稳定 Windows 路径形式。"""
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(path.strip()))


def automation_eligible(info: WindowInfo) -> bool:
    """自动规则只允许标准、可交互的主任务窗口。

    手动居中仍保持宽松；这些限制只用于会在后台自动触发的规则，避免
    同进程的通知、浮层和对话框被误当作主窗口。
    """
    if not info.exe_path or not info.class_name:
        return False
    if info.pid == _own_pid():
        return False
    own_exe = _own_exe()
    if own_exe and normalized_exe_path(info.exe_path) == normalized_exe_path(own_exe):
        return False
    if info.class_name in SHELL_CLASSES or info.style & W.WS_CHILD:
        return False
    if int(W.user32.GetAncestor(info.hwnd, W.GA_ROOT) or 0) != info.hwnd:
        return False
    if info.ex_style & (W.WS_EX_TOOLWINDOW | W.WS_EX_NOACTIVATE):
        return False
    # 有 owner 的顶层窗口通常是对话框或临时弹窗；显式 APPWINDOW 除外。
    if info.owner and not (info.ex_style & W.WS_EX_APPWINDOW):
        return False
    return True


def _ignored(info: WindowInfo, ignore_list: list[str]) -> bool:
    if not ignore_list:
        return False
    name = info.exe_name.lower()
    path = info.exe_path.lower()
    for entry in ignore_list:
        e = entry.strip().lower()
        if not e:
            continue
        if e == name or e == path or (name and e in name):
            return True
    return False


def list_windows(ignore_list: list[str] | None = None,
                 include_minimized: bool = True,
                 allow_own: bool = False) -> list[WindowInfo]:
    """按 Z 序（从上到下）枚举可操作的应用窗口。

    allow_own=True 时把 Aplomb 自己的窗口也算进来（设置界面）；
    自己的临时浮层（选择器）因为带 WS_EX_TOOLWINDOW，仍会被过滤掉。
    """
    results: list[WindowInfo] = []
    ignore = ignore_list or []

    def _callback(hwnd, _lparam):
        info = inspect(hwnd, allow_own=allow_own)
        if info is None:
            return True
        if _ignored(info, ignore):
            return True
        if not include_minimized and info.minimized:
            return True
        results.append(info)
        return True

    W.user32.EnumWindows(W.WNDENUMPROC(_callback), 0)
    return results


# --------------------------------------------------------------------------- #
# 居中
# --------------------------------------------------------------------------- #
def _target_rect(info: WindowInfo, pixel_perfect: bool) -> tuple[int, int, int, int]:
    """算目标位置。几何一律现取，不用缓存值 —— 否则连续按两次会算错阴影偏移。"""
    work = info.work_rect
    rect = _effective_rect(info.hwnd) or info.rect

    if pixel_perfect:
        frame = W.get_visible_frame(info.hwnd)
        if frame is not None:
            dl = frame.left - rect.left
            dt = frame.top - rect.top
            dr = rect.right - frame.right
            db = rect.bottom - frame.bottom
            if max(abs(dl), abs(dt), abs(dr), abs(db)) <= MAX_SHADOW_OFFSET:
                frame_x = work.left + (work.width - frame.width) // 2
                frame_y = work.top + (work.height - frame.height) // 2
                return frame_x - dl, frame_y - dt, rect.width, rect.height
        # 拿不到可见边框（最小化），或偏移大得不像阴影 —— 退回矩形居中
    x = work.left + (work.width - rect.width) // 2
    y = work.top + (work.height - rect.height) // 2
    return x, y, rect.width, rect.height


def _center_restore_position(hwnd: int) -> bool:
    """不动最小化/最大化状态，只把将来的还原落点改到工作区正中。

    对非普通状态的窗口走 SetWindowPos 既可能碰到 -32000 停车位，也可能
    意外改变显示状态。SetWindowPlacement 可以只改 rcNormalPosition，并保留
    原有 showCmd，因此不会让窗口莫名最大化、还原或退出全屏。
    """
    geometry = _restore_geometry(hwnd)
    if geometry is None:
        return False
    normal, monitor = geometry
    work = monitor.rcWork
    x = work.left + (work.width - normal.width) // 2
    y = work.top + (work.height - normal.height) // 2
    placement = W.get_placement(hwnd)
    if placement is None:
        return False
    placement.rcNormalPosition = _screen_to_work(
        W.RECT(x, y, x + normal.width, y + normal.height), monitor
    )
    return W.set_placement(hwnd, placement)


def center(info: WindowInfo, pixel_perfect: bool = True,
           on_centered=None) -> tuple[bool, str]:
    """把单个窗口居中，返回 (是否成功, 原因)。

    on_centered 在成功移动后以 WindowInfo 回调一次，供可选的精确规则收录。
    """
    if info.class_name in IMMOVABLE_CLASSES:
        return False, "系统窗口不参与居中"
    if info.fullscreen:
        return False, "全屏窗口已跳过"

    hwnd = info.hwnd

    if info.minimized or info.maximized:
        if _center_restore_position(hwnd):
            if on_centered is not None:
                on_centered(info)
            return True, ""
        if ctypes.get_last_error() == W.ERROR_ACCESS_DENIED:
            return False, ELEVATION_REQUIRED
        return False, "系统拒绝了这次移动"

    rect = _effective_rect(hwnd)
    if rect is None or rect.left <= -30000 or rect.width <= 0 or rect.height <= 0:
        return False, "窗口几何暂时不可用，已跳过"
    info.rect = rect

    x, y, cx, cy = _target_rect(info, pixel_perfect)
    flags = W.SWP_NOZORDER | W.SWP_NOACTIVATE
    if (rect.width, rect.height) == (cx, cy):
        # 纯移动：尺寸不变就不重设，目标应用不会收到多余的 WM_SIZE
        flags |= W.SWP_NOSIZE
    if not W.set_window_pos(hwnd, x, y, cx, cy, flags):
        if ctypes.get_last_error() == W.ERROR_ACCESS_DENIED:
            return False, ELEVATION_REQUIRED
        return False, "系统拒绝了这次移动"
    if on_centered is not None:
        on_centered(info)
    return True, ""


def belongs_to_us(hwnd: int) -> bool:
    """这个窗口是不是本程序自己的。"""
    if not hwnd:
        return False
    pid = wintypes.DWORD(0)
    W.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == _own_pid()


def foreground_info(fallback_hwnd: int = 0) -> tuple[WindowInfo | None, str]:
    """返回当前可操作窗口；托盘抢走焦点时退回最近的有效窗口。

    如果焦点落在 Aplomb 自己身上（比如正在看设置界面），也照样居中它自己 ——
    「把当前这个窗口摆正」是最符合直觉的行为。但批量居中和选择器列表里
    不会包含自己，避免误伤。
    """
    hwnd = int(W.user32.GetForegroundWindow() or 0)
    if not hwnd:
        return None, "没有找到活动窗口"

    info = inspect(hwnd, allow_own=True)
    if info is not None:
        return info, ""

    # 打开托盘菜单后，前台窗口可能变成任务栏或本进程的临时菜单。
    # 只回退到 WinEvent 记录的最近有效顶层窗口，不根据 Z 序猜测。
    if fallback_hwnd and fallback_hwnd != hwnd:
        fallback = inspect(fallback_hwnd, allow_own=False)
        if fallback is not None:
            return fallback, ""

    # 自己的浮层（选择器等）不参与居中，这种情况静默跳过
    if belongs_to_us(hwnd):
        return None, ""
    return None, "当前窗口不支持居中"


def center_foreground(pixel_perfect: bool = True,
                      on_centered=None,
                      fallback_hwnd: int = 0) -> tuple[bool, str]:
    """居中当前窗口，并支持托盘菜单导致的焦点回退。"""
    info, reason = foreground_info(fallback_hwnd)
    if info is None:
        return False, reason
    return center(info, pixel_perfect, on_centered=on_centered)


def center_all(pixel_perfect: bool = True,
               ignore_list: list[str] | None = None,
               on_centered=None,
               on_access_denied=None) -> tuple[int, int]:
    """批量居中。跳过最小化、最大化、全屏窗口 —— 不做惊喜式打扰。

    自己的设置窗口同样会被居中（三种模式行为保持一致）。
    """
    count = 0
    total = 0
    denied: list[WindowInfo] = []
    for info in list_windows(ignore_list, include_minimized=False, allow_own=True):
        if info.fullscreen or info.maximized or info.minimized:
            continue
        total += 1
        ok, reason = center(info, pixel_perfect, on_centered=on_centered)
        if ok:
            count += 1
        elif reason == ELEVATION_REQUIRED:
            denied.append(info)
    if denied and on_access_denied is not None:
        count += max(0, int(on_access_denied(denied)))
    return count, total


def activate(hwnd: int) -> None:
    """把窗口带到前台（我们此刻是前台，所以这个调用会被允许）。"""
    W.user32.SetForegroundWindow(hwnd)
