"""本次运行期间复用的管理员辅助进程。

主程序始终保持普通权限。第一次手动操作被 UIPI 拒绝时，按需启动同一个
Aplomb.exe 的管理员 broker 模式；后续手动请求通过带随机认证密钥的本地
命名管道复用该进程。协议只接受窗口句柄和像素修正开关，使用 JSON 字节，
绝不在高权限进程中反序列化 pickle 或接受命令、路径与任意坐标。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Client, Listener
from pathlib import Path

from . import win32api as W
from . import windows as winlib

MAX_TARGETS = 64
MAX_MESSAGE_BYTES = 8192
BROKER_CONNECT_TIMEOUT = 8.0
BROKER_START_TIMEOUT = 12.0
BROKER_INVALID_REQUEST = 250
BROKER_INTERNAL_ERROR = 251
_PIPE_RE = re.compile(r"^\\\\\.\\pipe\\Aplomb\.(\d+)\.([0-9a-f]{32})$")


@dataclass(frozen=True, slots=True)
class ElevatedCenterResult:
    moved: int = 0
    cancelled: bool = False
    error: int = 0


def _clean_hwnds(hwnds) -> list[int]:
    if not isinstance(hwnds, list) or len(hwnds) > MAX_TARGETS:
        return []
    result: list[int] = []
    for hwnd in hwnds:
        if type(hwnd) is not int or hwnd <= 0:
            return []
        if hwnd not in result:
            result.append(hwnd)
    return result


def _encode(message: dict) -> bytes:
    return json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _decode(payload: bytes) -> dict | None:
    if not payload or len(payload) > MAX_MESSAGE_BYTES:
        return None
    try:
        value = json.loads(payload.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _inspect_target(hwnd: int):
    """每次请求都重新验证句柄，绝不信任连接建立时的旧状态。"""
    if int(W.user32.GetAncestor(hwnd, W.GA_ROOT) or 0) != hwnd:
        return None
    return winlib.inspect(hwnd)


def _center_targets(hwnds: list[int], pixel_perfect: bool) -> tuple[int, int]:
    moved = 0
    for hwnd in hwnds:
        info = _inspect_target(hwnd)
        if info is None:
            continue
        ok, _reason = winlib.center(info, pixel_perfect)
        if ok:
            moved += 1
    return moved, len(hwnds)


def _parse_broker_args(args: list[str]) -> tuple[str, bytes, int] | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--elevated-broker", action="store_true")
    parser.add_argument("--broker-pipe")
    parser.add_argument("--broker-key")
    parser.add_argument("--parent-pid", type=int)
    try:
        options, unknown = parser.parse_known_args(args)
    except SystemExit:
        return None
    if unknown or not options.elevated_broker:
        return None
    if not options.broker_pipe or not options.broker_key or not options.parent_pid:
        return None
    match = _PIPE_RE.fullmatch(options.broker_pipe)
    if match is None or int(match.group(1)) != options.parent_pid:
        return None
    if not re.fullmatch(r"[0-9a-f]{64}", options.broker_key):
        return None
    try:
        key = bytes.fromhex(options.broker_key)
    except ValueError:
        return None
    if len(key) != 32 or options.parent_pid <= 0:
        return None
    return options.broker_pipe, key, options.parent_pid


def _watch_parent(parent_handle) -> None:
    """主程序消失时立即结束 broker，包括尚未连上管道的启动阶段。"""
    if W.kernel32.WaitForSingleObject(parent_handle, W.INFINITE) == W.WAIT_OBJECT_0:
        os._exit(0)


def _receive(connection) -> dict | None:
    return _decode(connection.recv_bytes(MAX_MESSAGE_BYTES))


def _send(connection, message: dict) -> None:
    connection.send_bytes(_encode(message))


def run_broker(args: list[str]) -> int:
    """管理员 broker 入口；单连接、有限协议，随主程序生命周期退出。"""
    parsed = _parse_broker_args(args)
    if parsed is None:
        return BROKER_INVALID_REQUEST
    pipe_name, authkey, parent_pid = parsed

    parent_handle = W.kernel32.OpenProcess(W.SYNCHRONIZE, False, parent_pid)
    if not parent_handle:
        return BROKER_INVALID_REQUEST
    threading.Thread(
        target=_watch_parent,
        args=(parent_handle,),
        name="AplombParentWatch",
        daemon=True,
    ).start()

    connection = None
    try:
        # 管道必须由普通权限主程序创建。高权限进程创建的管道带高完整性标签，
        # 普通进程会被“禁止向上写入”拦截，表现为授权成功却永远连接不上。
        deadline = time.monotonic() + BROKER_CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            try:
                connection = Client(address=pipe_name, family="AF_PIPE", authkey=authkey)
                break
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                time.sleep(0.05)
        if connection is None:
            return BROKER_INTERNAL_ERROR

        _send(connection, {"op": "hello", "pid": parent_pid})
        ready = _receive(connection)
        if ready != {"ok": True, "ready": True}:
            return BROKER_INVALID_REQUEST

        while True:
            request = _receive(connection)
            if request is None:
                _send(connection, {"ok": False, "error": BROKER_INVALID_REQUEST})
                continue
            if request == {"op": "shutdown"}:
                _send(connection, {"ok": True})
                return 0
            if set(request) != {"op", "hwnds", "pixel_perfect"}:
                _send(connection, {"ok": False, "error": BROKER_INVALID_REQUEST})
                continue
            if request.get("op") != "center" or type(request.get("pixel_perfect")) is not bool:
                _send(connection, {"ok": False, "error": BROKER_INVALID_REQUEST})
                continue
            hwnds = _clean_hwnds(request.get("hwnds"))
            if not hwnds:
                _send(connection, {"ok": False, "error": BROKER_INVALID_REQUEST})
                continue
            moved, requested = _center_targets(hwnds, request["pixel_perfect"])
            _send(connection, {"ok": True, "moved": moved, "requested": requested})
    except (EOFError, OSError, BrokenPipeError):
        # 管道断开通常表示主程序退出或崩溃；broker 不得继续驻留。
        return 0
    except Exception:
        return BROKER_INTERNAL_ERROR
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


def _broker_command(pipe_name: str, authkey: bytes, parent_pid: int) -> tuple[str, str, str]:
    executable = str(Path(sys.executable).resolve())
    args: list[str] = []
    if not getattr(sys, "frozen", False):
        args.append(str(Path(__file__).resolve().parent.parent / "main.py"))
    args.extend([
        "--elevated-broker",
        "--broker-pipe", pipe_name,
        "--broker-key", authkey.hex(),
        "--parent-pid", str(parent_pid),
    ])
    return executable, subprocess.list2cmdline(args), str(Path(executable).parent)


def _shell_execute(info: W.SHELLEXECUTEINFOW) -> bool:
    return bool(W.shell32.ShellExecuteExW(ctypes.byref(info)))


def _process_exit_code(handle) -> int | None:
    code = W.w.DWORD(W.STILL_ACTIVE)
    if not W.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
        return None
    return int(code.value)


class BrokerClient:
    """普通权限主程序持有的会话级客户端；第一次需要时才启动 broker。"""

    def __init__(self, owner_hwnd: int = 0) -> None:
        self.owner_hwnd = owner_hwnd
        self._connection = None
        self._process_handle = None

    @property
    def active(self) -> bool:
        return self._connection is not None

    def _discard_process(self, *, terminate: bool) -> None:
        handle = self._process_handle
        self._process_handle = None
        if not handle:
            return
        try:
            if terminate and W.kernel32.WaitForSingleObject(handle, 0) == W.WAIT_TIMEOUT:
                W.kernel32.TerminateProcess(handle, BROKER_INTERNAL_ERROR)
                W.kernel32.WaitForSingleObject(handle, 2000)
        finally:
            W.kernel32.CloseHandle(handle)

    def _disconnect(self, *, terminate: bool) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        self._discard_process(terminate=terminate)

    def _start(self) -> ElevatedCenterResult:
        parent_pid = os.getpid()
        pipe_name = rf"\\.\pipe\Aplomb.{parent_pid}.{secrets.token_hex(16)}"
        authkey = secrets.token_bytes(32)
        executable, parameters, directory = _broker_command(pipe_name, authkey, parent_pid)

        # 先由普通权限进程创建管道，再启动管理员进程向下连接。
        try:
            listener = Listener(address=pipe_name, family="AF_PIPE", authkey=authkey)
        except OSError:
            return ElevatedCenterResult(error=BROKER_INTERNAL_ERROR)

        accepted: queue.Queue = queue.Queue(maxsize=1)

        def accept_once() -> None:
            try:
                accepted.put((True, listener.accept()))
            except BaseException as error:
                accepted.put((False, error))

        threading.Thread(
            target=accept_once,
            name="AplombBrokerAccept",
            daemon=True,
        ).start()

        info = W.SHELLEXECUTEINFOW()
        info.cbSize = ctypes.sizeof(W.SHELLEXECUTEINFOW)
        info.fMask = W.SEE_MASK_NOCLOSEPROCESS | W.SEE_MASK_NOASYNC | W.SEE_MASK_FLAG_NO_UI
        info.hwnd = self.owner_hwnd
        info.lpVerb = "runas"
        info.lpFile = executable
        info.lpParameters = parameters
        info.lpDirectory = directory
        info.nShow = W.SW_HIDE

        ctypes.set_last_error(0)
        if not _shell_execute(info):
            error = ctypes.get_last_error()
            listener.close()
            return ElevatedCenterResult(cancelled=error == W.ERROR_CANCELLED, error=error)
        if not info.hProcess:
            listener.close()
            return ElevatedCenterResult(error=BROKER_INTERNAL_ERROR)
        self._process_handle = info.hProcess

        deadline = time.monotonic() + BROKER_START_TIMEOUT
        connection = None
        while time.monotonic() < deadline:
            try:
                accepted_ok, accepted_value = accepted.get_nowait()
                if accepted_ok:
                    connection = accepted_value
                break
            except queue.Empty:
                pass
            code = _process_exit_code(info.hProcess)
            if code is not None and code != W.STILL_ACTIVE:
                listener.close()
                self._discard_process(terminate=False)
                return ElevatedCenterResult(error=code or BROKER_INTERNAL_ERROR)
            time.sleep(0.05)
        listener.close()
        if connection is None:
            # broker 的连接超时短于这里，它会自行退出；不要只杀 PyInstaller
            # 外层进程，否则可能留下仍在高权限运行的解包子进程。
            W.kernel32.WaitForSingleObject(info.hProcess, 2000)
            self._discard_process(terminate=False)
            return ElevatedCenterResult(error=BROKER_INTERNAL_ERROR)

        self._connection = connection
        try:
            hello = _receive(connection)
            if hello != {"op": "hello", "pid": parent_pid}:
                self._disconnect(terminate=True)
                return ElevatedCenterResult(error=BROKER_INTERNAL_ERROR)
            _send(connection, {"ok": True, "ready": True})
        except (EOFError, OSError, BrokenPipeError):
            self._disconnect(terminate=True)
            return ElevatedCenterResult(error=BROKER_INTERNAL_ERROR)
        return ElevatedCenterResult()

    def center(self, hwnds: list[int], pixel_perfect: bool) -> ElevatedCenterResult:
        clean = _clean_hwnds(hwnds)
        if not clean:
            return ElevatedCenterResult(error=BROKER_INVALID_REQUEST)
        if self._connection is None:
            started = self._start()
            if started.cancelled or started.error:
                return started

        try:
            _send(self._connection, {
                "op": "center",
                "hwnds": clean,
                "pixel_perfect": bool(pixel_perfect),
            })
            reply = _receive(self._connection)
            if not reply or reply.get("ok") is not True:
                return ElevatedCenterResult(error=int((reply or {}).get("error", BROKER_INTERNAL_ERROR)))
            moved = reply.get("moved")
            requested = reply.get("requested")
            if type(moved) is not int or type(requested) is not int or requested != len(clean):
                raise ValueError("invalid broker response")
            return ElevatedCenterResult(moved=max(0, min(moved, len(clean))))
        except (EOFError, OSError, BrokenPipeError, ValueError, TypeError):
            self._disconnect(terminate=True)
            return ElevatedCenterResult(error=BROKER_INTERNAL_ERROR)

    def close(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                _send(connection, {"op": "shutdown"})
            except (EOFError, OSError, BrokenPipeError):
                pass
            try:
                connection.close()
            except OSError:
                pass

        handle = self._process_handle
        if handle:
            if W.kernel32.WaitForSingleObject(handle, 2000) == W.WAIT_TIMEOUT:
                W.kernel32.TerminateProcess(handle, 0)
                W.kernel32.WaitForSingleObject(handle, 2000)
        self._discard_process(terminate=False)
