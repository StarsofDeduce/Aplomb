"""精确、一次性的自动居中规则。

规则由完整可执行文件路径、窗口类名和稳定的窗口结构签名组成，只匹配标准
主任务窗口。SHOW 事件后短暂等待启动几何稳定，再执行一次。Aplomb 不会在
移动成功后继续跟踪位置，也不会和应用或用户后续的位置调整争抢。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal

from . import win32api as W
from . import windows as winlib
from .win32api import (
    EVENT_OBJECT_CREATE,
    EVENT_OBJECT_DESTROY,
    EVENT_OBJECT_SHOW,
    OBJID_WINDOW,
    WINEVENT_OUTOFCONTEXT,
    WINEVENT_SKIPOWNPROCESS,
)


def rule_key(rule: dict) -> tuple[str, str]:
    """规则的大小写不敏感唯一键；无效规则返回空键。"""
    if not isinstance(rule, dict):
        return "", ""
    path = winlib.normalized_exe_path(str(rule.get("exe_path", "")))
    class_name = str(rule.get("class_name", "")).strip().casefold()
    return path, class_name


def valid_rules(raw) -> list[dict]:
    """过滤损坏或重复的配置项，不让手改 JSON 影响事件回调。"""
    if not isinstance(raw, list):
        return []
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        key = rule_key(item)
        if not all(key) or key in seen:
            continue
        seen.add(key)
        rule = {
            "exe_path": str(item.get("exe_path", "")).strip(),
            "exe_name": str(item.get("exe_name", "")).strip(),
            "class_name": str(item.get("class_name", "")).strip(),
        }
        style_signature = item.get("style_signature")
        ex_style_signature = item.get("ex_style_signature")
        if (isinstance(style_signature, int)
                and isinstance(ex_style_signature, int)):
            rule["style_signature"] = style_signature
            rule["ex_style_signature"] = ex_style_signature
        result.append(rule)
    return result


def rule_from_info(info) -> dict | None:
    """从用户明确选中的标准主窗口生成精确规则。"""
    if info is None or not winlib.automation_eligible(info):
        return None
    style_signature, ex_style_signature = winlib.automation_signature(info)
    return {
        "exe_path": info.exe_path,
        "exe_name": info.exe_name,
        "class_name": info.class_name,
        "style_signature": style_signature,
        "ex_style_signature": ex_style_signature,
    }


def add_rule(config, info) -> bool:
    """添加精确规则；旧 exe 名单始终不参与匹配。"""
    rule = rule_from_info(info)
    if rule is None:
        return False

    rules = valid_rules(config.get("remember_rules", []))
    key = rule_key(rule)
    for index, existing in enumerate(rules):
        if rule_key(existing) != key:
            continue
        if existing == rule:
            return False
        # 同一路径和类名只保留用户最后明确选择的窗口角色；这也会把
        # 1.2.0-1.2.2 的旧规则原地升级为带样式签名的新规则。
        rules[index] = rule
        config.set("remember_rules", rules)
        return True
    rules.append(rule)
    config.set("remember_rules", rules)
    return True


class RememberManager(QObject):
    rules_changed = Signal()

    def __init__(self, config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        # ctypes 回调必须由 Python 对象持有，否则回收后会导致进程崩溃。
        self._proc = W.WINEVENTPROC(self._on_event)
        self._hook = None
        # pending 保存同一 HWND 当前最晚的阶段；SHOW 可以覆盖尚未处理的 CREATE。
        self._pending: dict[int, int] = {}
        self._handled: set[int] = set()
        # HWND 会被 Windows 复用；令牌确保旧生命周期残留的计时器无法
        # 作用到恰好拿到同一数值句柄的新窗口。
        self._settling: dict[int, int] = {}
        self._next_token = 0
        self.sync()

    def sync(self) -> None:
        """按总开关和有效规则装卸事件钩子。"""
        wanted = (
            bool(self.config.get("remember_centered", True))
            and bool(valid_rules(self.config.get("remember_rules", [])))
        )
        if wanted and self._hook is None:
            self._hook = W.user32.SetWinEventHook(
                EVENT_OBJECT_CREATE,
                EVENT_OBJECT_SHOW,
                None,
                self._proc,
                0,
                0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
            )
        elif not wanted and self._hook is not None:
            W.user32.UnhookWinEvent(self._hook)
            self._hook = None
        if not wanted:
            self._pending.clear()
            self._handled.clear()
            self._settling.clear()

    def record(self, info) -> None:
        """按独立开关把一次手动单窗口操作收录为精确规则。"""
        if not self.config.get("remember_after_manual_center", False):
            return
        if add_rule(self.config, info):
            self.rules_changed.emit()
            self.sync()

    def _on_event(self, _hook, event, hwnd, id_object, id_child,
                  _thread, _time_ms) -> None:
        # 全局事件回调只做排队，所有检查和移动都回到 Qt 事件循环执行。
        try:
            if id_object != OBJID_WINDOW or id_child != 0 or not hwnd:
                return
            hwnd = int(hwnd)
            if event == EVENT_OBJECT_DESTROY:
                self._pending.pop(hwnd, None)
                self._handled.discard(hwnd)
                self._settling.pop(hwnd, None)
                return
            if event not in (EVENT_OBJECT_CREATE, EVENT_OBJECT_SHOW):
                return
            if hwnd in self._handled or hwnd in self._settling:
                return
            if hwnd in self._pending:
                if event == EVENT_OBJECT_SHOW:
                    self._pending[hwnd] = event
                return
            self._pending[hwnd] = event
            QTimer.singleShot(0, lambda h=hwnd: self._apply(h))
        except Exception:
            pass

    def _apply(self, hwnd: int) -> None:
        event = self._pending.pop(hwnd, None)
        if event is None or hwnd in self._handled:
            return
        shown = event == EVENT_OBJECT_SHOW
        try:
            # CREATE 时目标程序往往尚未恢复自己的保存位置。此时移动虽然
            # 会返回成功，却马上被程序覆盖，随后还会因“已处理”而漏掉 SHOW。
            # 只把 CREATE 当作观察信号，实际移动统一留到 SHOW 后的稳定阶段。
            if not shown:
                return
            info = winlib.inspect(
                hwnd,
                allow_own=False,
                require_visible=True,
                require_title=False,
            )
            if info is None or not winlib.automation_eligible(info):
                self._handled.add(hwnd)
                return
            # SHOW 到达时有些应用尚未补齐最终样式。先按路径和类名锁定
            # 候选，稳定后再核对角色签名，避免主窗口被过早判为不匹配。
            if not self._base_matches(info):
                self._handled.add(hwnd)
                return
            self._next_token += 1
            token = self._next_token
            self._settling[hwnd] = token
            QTimer.singleShot(
                40,
                lambda h=hwnd, t=token: self._wait_until_stable(h, t),
            )
        except Exception:
            self._handled.add(hwnd)

    def _wait_until_stable(
        self,
        hwnd: int,
        token: int,
        previous: tuple[int, int, int, int] | None = None,
        stable_samples: int = 0,
        attempts: int = 0,
    ) -> None:
        """只读等待启动几何稳定，然后恰好尝试移动一次。

        每 40ms 采样，连续四次一致（约 120ms 静止）即执行；若目标一直
        调整布局，最多等待约 800ms。这里不会在移动后继续跟踪或拉回。
        """
        if self._settling.get(hwnd) != token or hwnd in self._handled:
            return
        try:
            info = winlib.inspect(
                hwnd,
                allow_own=False,
                require_visible=True,
                require_title=False,
            )
            if (info is None or not winlib.automation_eligible(info)
                    or not self._base_matches(info)):
                self._finish(hwnd, token)
                return
            rect = info.rect
            current = (rect.left, rect.top, rect.right, rect.bottom)
            stable_samples = stable_samples + 1 if current == previous else 1
            attempts += 1
            if stable_samples >= 4 or attempts >= 20:
                if not self._matches(info):
                    self._finish(hwnd, token)
                    return
                self._upgrade_legacy_rule(info)
                winlib.center(
                    info,
                    self.config.get("pixel_perfect", True),
                )
                self._finish(hwnd, token)
                return
            QTimer.singleShot(
                40,
                lambda h=hwnd, t=token, r=current, s=stable_samples, a=attempts:
                    self._wait_until_stable(h, t, r, s, a),
            )
        except Exception:
            self._finish(hwnd, token)

    def _finish(self, hwnd: int, token: int) -> None:
        if self._settling.get(hwnd) != token:
            return
        self._settling.pop(hwnd, None)
        self._handled.add(hwnd)

    def _matches(self, info) -> bool:
        if not self._base_matches(info):
            return False
        target = (
            winlib.normalized_exe_path(info.exe_path),
            info.class_name.strip().casefold(),
        )
        signature = winlib.automation_signature(info)
        for rule in valid_rules(self.config.get("remember_rules", [])):
            if rule_key(rule) != target:
                continue
            saved = (rule.get("style_signature"), rule.get("ex_style_signature"))
            if all(isinstance(value, int) for value in saved):
                return saved == signature
            return winlib.legacy_primary_candidate(info)
        return False

    def _base_matches(self, info) -> bool:
        """仅匹配忽略优先级、路径和类名；用于等待候选稳定。"""
        if winlib._ignored(info, self.config.get("ignore_list", [])):
            return False
        target = (
            winlib.normalized_exe_path(info.exe_path),
            info.class_name.strip().casefold(),
        )
        for rule in valid_rules(self.config.get("remember_rules", [])):
            if rule_key(rule) == target:
                return True
        return False

    def _upgrade_legacy_rule(self, info) -> None:
        """首次可信命中时原地补全旧规则，不清空用户名单。"""
        rules = valid_rules(self.config.get("remember_rules", []))
        target = rule_key({
            "exe_path": info.exe_path,
            "class_name": info.class_name,
        })
        changed = False
        replacement = rule_from_info(info)
        if replacement is None:
            return
        for index, rule in enumerate(rules):
            if rule_key(rule) != target:
                continue
            if "style_signature" not in rule or "ex_style_signature" not in rule:
                rules[index] = replacement
                changed = True
            break
        if changed:
            self.config.set("remember_rules", rules)
            self.rules_changed.emit()
