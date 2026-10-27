"""精确自动居中端到端测试：主窗口命中，通知不动，且只移动一次。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

_config_temp = tempfile.TemporaryDirectory(prefix="aplomb-remember-")
os.environ["APLOMB_CONFIG_DIR"] = _config_temp.name

from PySide6.QtWidgets import QApplication

from app import win32api as W
from app import windows as winlib
from app.config import Config
from app.remember import RememberManager, add_rule, valid_rules

app = QApplication(sys.argv[:1])
ok = True


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok
    if not condition:
        ok = False
    print(f"[{'PASS' if condition else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")


def find_title(title: str) -> int:
    found: list[int] = []

    def callback(hwnd, _):
        if W.get_window_text(hwnd) == title:
            found.append(int(hwnd))
            return False
        return True

    W.user32.EnumWindows(W.WNDENUMPROC(callback), 0)
    return found[0] if found else 0


def wait_title(title: str, timeout: float = 6.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        hwnd = find_title(title)
        if hwnd:
            return hwnd
        time.sleep(0.03)
    return 0


def stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


target_script = str(BASE / "tools" / "_target_window.py")
first = subprocess.Popen(
    [sys.executable, target_script, "--lifetime-ms", "12000"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
manager = None

try:
    first_hwnd = wait_title("Aplomb Test Target")
    first_info = winlib.inspect(first_hwnd) if first_hwnd else None
    check("测试主窗口可识别", first_info is not None)
    check("测试主窗口可用于自动规则",
          first_info is not None and winlib.automation_eligible(first_info))

    cfg = Config()
    cfg.reset()
    if first_info is not None:
        cfg.set("remembered_apps", [first_info.exe_name])
        cfg = Config()
        check("旧 exe 名单升级时永久清空", cfg.get("remembered_apps") == [])
        saved = json.loads(cfg.path.read_text(encoding="utf-8"))
        check("旧名单清空已原子写回配置", saved.get("remembered_apps") == [])
        manager = RememberManager(cfg)
        check("没有精确规则时不匹配", not manager._matches(first_info))
        check("精确规则创建成功", add_rule(cfg, first_info))
        saved_rules = valid_rules(cfg.get("remember_rules"))
        check("精确规则持久化", len(saved_rules) == 1)
        check(
            "新规则保存稳定窗口结构签名",
            len(saved_rules) == 1
            and "style_signature" in saved_rules[0]
            and "ex_style_signature" in saved_rules[0],
        )
        manager.sync()

        different_class = replace(first_info, class_name="AplombDifferentWindowClass")
        check("同一路径不同窗口类不匹配", not manager._matches(different_class))

        # 模拟 1.2.0-1.2.2 仅含路径和类名的规则，下一次可信主窗口
        # 命中时应自动补全签名，不要求用户删除后重新添加。
        legacy_rule = dict(saved_rules[0])
        legacy_rule.pop("style_signature", None)
        legacy_rule.pop("ex_style_signature", None)
        cfg.set("remember_rules", [legacy_rule])
        check("旧规则仍可识别可信主窗口", manager._matches(first_info))
finally:
    stop(first)

# 管理器必须先于目标窗口启动，才能观察 CREATE/SHOW。
if manager is None:
    manager = RememberManager(cfg)
second = subprocess.Popen(
    [sys.executable, target_script, "--popup", "--same-class-feature",
     "--restore-after-show-ms", "90", "--delay-ms", "350",
     "--lifetime-ms", "12000"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)

try:
    main_hwnd = wait_title("Aplomb Test Target")
    notice_hwnd = wait_title("Aplomb Test Notice")
    feature_hwnd = wait_title("Aplomb Test Feature")
    feature_before = W.get_window_rect(feature_hwnd) if feature_hwnd else None
    deadline = time.monotonic() + 1.2
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)

    main_info = winlib.inspect(main_hwnd) if main_hwnd else None
    feature_info = winlib.inspect(feature_hwnd) if feature_hwnd else None
    main_rect = W.get_visible_frame(main_hwnd) if main_hwnd else None
    notice_rect = W.get_window_rect(notice_hwnd) if notice_hwnd else None
    feature_after = W.get_window_rect(feature_hwnd) if feature_hwnd else None
    check("精确规则目标窗口已出现", main_info is not None)
    check("同进程通知窗口已出现", notice_rect is not None)
    check("同类功能窗口已出现", feature_after is not None)
    check(
        "功能窗确实与主窗口共用窗口类",
        main_info is not None
        and feature_info is not None
        and feature_info.class_name == main_info.class_name,
        "" if main_info is None or feature_info is None else
        f"main={main_info.class_name}, feature={feature_info.class_name}",
    )
    check(
        "功能窗能通过旧版基础资格筛选",
        feature_info is not None
        and winlib.automation_eligible(feature_info)
        and manager._base_matches(feature_info),
    )
    check(
        "窗口结构签名排除同类功能窗",
        feature_info is not None and not manager._matches(feature_info),
    )
    migrated_rules = valid_rules(cfg.get("remember_rules"))
    check(
        "旧规则首次命中后自动补全窗口结构签名",
        len(migrated_rules) == 1
        and "style_signature" in migrated_rules[0]
        and "ex_style_signature" in migrated_rules[0],
    )

    if main_info is not None and main_rect is not None:
        work = main_info.work_rect
        dx = (main_rect.left + main_rect.right) / 2 - (work.left + work.right) / 2
        dy = (main_rect.top + main_rect.bottom) / 2 - (work.top + work.bottom) / 2
        check("主窗口被精确规则居中", abs(dx) <= 1 and abs(dy) <= 1,
              f"dx={dx:.1f}, dy={dy:.1f}")

    if notice_rect is not None:
        monitor = W.get_monitor_info(notice_hwnd)
        near_right = monitor is not None and monitor.rcWork.right - notice_rect.right <= 40
        near_bottom = monitor is not None and monitor.rcWork.bottom - notice_rect.bottom <= 40
        check("通知窗口保持右下角", near_right and near_bottom,
              f"rect=({notice_rect.left},{notice_rect.top},{notice_rect.right},{notice_rect.bottom})")
        notice_info = winlib.inspect(notice_hwnd)
        check("通知窗口不具备自动规则资格", notice_info is None)

    if feature_before is not None and feature_after is not None:
        check(
            "同路径同类名但窗口结构不同的功能窗不移动",
            (feature_before.left, feature_before.top) ==
            (feature_after.left, feature_after.top),
            f"before={(feature_before.left, feature_before.top)}, "
            f"after={(feature_after.left, feature_after.top)}",
        )

    if main_hwnd:
        before = W.get_window_rect(main_hwnd)
        W.set_window_pos(main_hwnd, 97, 123, before.width, before.height)
        deadline = time.monotonic() + 0.6
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.02)
        after = W.get_window_rect(main_hwnd)
        check("窗口后续移动不会被拉回",
              after is not None and abs(after.left - 97) <= 1 and abs(after.top - 123) <= 1,
              f"after={None if after is None else (after.left, after.top)}")

    # 自动收录开关独立于自动执行总开关。
    cfg.set("remember_rules", [])
    cfg.set("remember_after_manual_center", False)
    manager.sync()
    manager.record(main_info)
    check("自动收录关闭时不创建规则", cfg.get("remember_rules") == [])
    cfg.set("remember_after_manual_center", True)
    manager.record(main_info)
    check("自动收录开启时创建精确规则",
          len(valid_rules(cfg.get("remember_rules"))) == 1)
finally:
    stop(second)

print("\n结果:", "全部通过" if ok else "有失败项")
sys.exit(0 if ok else 1)
