"""会话级管理员辅助模式回归；测试命名管道，但不会真的弹出 UAC。"""

from __future__ import annotations

import ctypes
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from multiprocessing.connection import Listener
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app import elevated
from app import win32api as W

ok = True


def check(name: str, condition: bool, detail: str = "") -> None:
    global ok
    if not condition:
        ok = False
    suffix = f" — {detail}" if detail else ""
    print(f"[{'PASS' if condition else 'FAIL'}] {name}{suffix}")


# ------------------------------------------------------------------ 参数与协议
pid = os.getpid()
token = "1" * 32
key_hex = "2" * 64
pipe = rf"\\.\pipe\Aplomb.{pid}.{token}"
parsed = elevated._parse_broker_args([
    "--elevated-broker", "--broker-pipe", pipe,
    "--broker-key", key_hex, "--parent-pid", str(pid),
])
check("解析受限 broker 参数", parsed == (pipe, bytes.fromhex(key_hex), pid), repr(parsed))
check("管道内 PID 必须匹配主进程",
      elevated._parse_broker_args([
          "--elevated-broker", "--broker-pipe", rf"\\.\pipe\Aplomb.{pid + 1}.{token}",
          "--broker-key", key_hex, "--parent-pid", str(pid),
      ]) is None)
check("拒绝未知启动参数",
      elevated._parse_broker_args([
          "--elevated-broker", "--broker-pipe", pipe,
          "--broker-key", key_hex, "--parent-pid", str(pid), "--run", "cmd",
      ]) is None)
check("拒绝非 JSON 与超长消息", elevated._decode(b"pickle") is None
      and elevated._decode(b"{}" * elevated.MAX_MESSAGE_BYTES) is None)
check("窗口句柄去重且限制数量", elevated._clean_hwnds([101, 202, 101]) == [101, 202]
      and elevated._clean_hwnds(list(range(1, elevated.MAX_TARGETS + 2))) == [])


# ------------------------------------------------------------------ 高权限端重新检查句柄
original_inspect = elevated._inspect_target
original_center = elevated.winlib.center
try:
    elevated._inspect_target = lambda hwnd: SimpleNamespace(hwnd=hwnd) if hwnd == 101 else None
    elevated.winlib.center = lambda _info, _pixel: (True, "")
    moved, requested = elevated._center_targets([101, 202], True)
    check("每次请求重新验证目标窗口", (moved, requested) == (1, 2),
          repr((moved, requested)))
finally:
    elevated._inspect_target = original_inspect
    elevated.winlib.center = original_center


# ------------------------------------------------------------------ 客户端只启动一次并复用连接
class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.responses = [
            elevated._encode({"ok": True, "moved": 1, "requested": 1}),
            elevated._encode({"ok": True, "moved": 1, "requested": 1}),
        ]
        self.closed = False

    def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    def recv_bytes(self, _maxlength: int) -> bytes:
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


fake = FakeConnection()
client = elevated.BrokerClient()
starts = 0


def fake_start() -> elevated.ElevatedCenterResult:
    global starts
    starts += 1
    client._connection = fake
    return elevated.ElevatedCenterResult()


client._start = fake_start
first = client.center([101], True)
second = client.center([202], False)
client.close()
ops = [elevated._decode(payload).get("op") for payload in fake.sent]
check("本次运行只启动一次辅助进程", starts == 1, str(starts))
check("后续手动操作复用同一连接", first.moved == 1 and second.moved == 1,
      repr((first, second)))
check("退出时发送 shutdown 并关闭管道", ops == ["center", "center", "shutdown"]
      and fake.closed, repr(ops))


# ------------------------------------------------------------------ UAC 取消
original_shell = elevated._shell_execute
try:
    def cancelled(_info):
        ctypes.set_last_error(W.ERROR_CANCELLED)
        return False

    elevated._shell_execute = cancelled
    cancelled_result = elevated.BrokerClient()._start()
    check("用户取消 UAC 能被识别", cancelled_result.cancelled
          and cancelled_result.error == W.ERROR_CANCELLED, repr(cancelled_result))
finally:
    elevated._shell_execute = original_shell


# ------------------------------------------------------------------ 真实本地命名管道与生命周期
real_token = secrets.token_hex(16)
real_key = secrets.token_bytes(32)
real_pipe = rf"\\.\pipe\Aplomb.{pid}.{real_token}"
broker_prefix = [sys.executable, str(BASE / "main.py")]
if "--broker-exe" in sys.argv:
    broker_prefix = [str(Path(sys.argv[sys.argv.index("--broker-exe") + 1]).resolve())]
listener = Listener(address=real_pipe, family="AF_PIPE", authkey=real_key)
accepted: queue.Queue = queue.Queue(maxsize=1)


def accept_broker() -> None:
    try:
        accepted.put(listener.accept())
    except BaseException:
        accepted.put(None)


threading.Thread(target=accept_broker, daemon=True).start()
broker = subprocess.Popen(broker_prefix + [
    "--elevated-broker",
    "--broker-pipe", real_pipe,
    "--broker-key", real_key.hex(),
    "--parent-pid", str(pid),
])
connection = None
try:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and broker.poll() is None:
        try:
            connection = accepted.get_nowait()
            break
        except queue.Empty:
            time.sleep(0.05)
    listener.close()
    check("真实 broker 建立认证命名管道", connection is not None,
          f"exit={broker.poll()}")
    if connection is not None:
        hello = elevated._receive(connection)
        check("broker 验证主进程身份", hello == {"op": "hello", "pid": pid}, repr(hello))
        elevated._send(connection, {"ok": True, "ready": True})

        elevated._send(connection, {
            "op": "center", "hwnds": [99999999], "pixel_perfect": True,
        })
        response = elevated._receive(connection)
        check("无效窗口只记为未移动而不扩大权限",
              response == {"ok": True, "moved": 0, "requested": 1}, repr(response))

        elevated._send(connection, {"op": "shutdown"})
        shutdown = elevated._receive(connection)
        check("broker 接受有限的正常退出请求", shutdown == {"ok": True}, repr(shutdown))
        connection.close()
        connection = None
    broker.wait(timeout=5)
    check("主程序退出流程不会留下 broker", broker.returncode == 0, str(broker.returncode))
finally:
    listener.close()
    if connection is not None:
        connection.close()
    if broker.poll() is None:
        broker.terminate()
        try:
            broker.wait(timeout=3)
        except subprocess.TimeoutExpired:
            broker.kill()


# 可选的打包后跨完整性测试：调用方明确传入 --uac-exe 时才会弹一次 UAC。
if "--uac-exe" in sys.argv:
    exe_index = sys.argv.index("--uac-exe") + 1
    packaged_exe = Path(sys.argv[exe_index]).resolve()
    target = subprocess.Popen(
        [sys.executable, str(BASE / "tools" / "_target_window.py"), "--lifetime-ms", "20000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    target_hwnd = 0
    uac_client = elevated.BrokerClient()
    original_command = elevated._broker_command
    try:
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not target_hwnd:
            found: list[int] = []

            def callback(hwnd, _):
                if W.get_window_text(hwnd) == "Aplomb Test Target":
                    found.append(int(hwnd))
                    return False
                return True

            W.user32.EnumWindows(W.WNDENUMPROC(callback), 0)
            target_hwnd = found[0] if found else 0
            time.sleep(0.05)
        check("打包后 UAC 测试窗口可识别", bool(target_hwnd))

        def packaged_command(pipe_name: str, authkey: bytes,
                             parent_pid: int) -> tuple[str, str, str]:
            args = [
                "--elevated-broker",
                "--broker-pipe", pipe_name,
                "--broker-key", authkey.hex(),
                "--parent-pid", str(parent_pid),
            ]
            return str(packaged_exe), subprocess.list2cmdline(args), str(packaged_exe.parent)

        elevated._broker_command = packaged_command
        first_uac = uac_client.center([target_hwnd], True)
        first_handle = int(uac_client._process_handle or 0)
        second_uac = uac_client.center([target_hwnd], True)
        second_handle = int(uac_client._process_handle or 0)
        check("打包版跨权限首次请求成功", first_uac.moved == 1, repr(first_uac))
        check("打包版第二次请求复用同一管理员进程",
              second_uac.moved == 1 and first_handle != 0 and first_handle == second_handle,
              repr((second_uac, first_handle, second_handle)))
    finally:
        elevated._broker_command = original_command
        uac_client.close()
        target.terminate()
        try:
            target.wait(timeout=3)
        except subprocess.TimeoutExpired:
            target.kill()

print("\n结果:", "全部通过" if ok else "有失败项")
raise SystemExit(0 if ok else 1)
