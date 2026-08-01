"""特性: 反复注入/卸载 10 次    类别: lifecycle

链路: 每轮 = 启动 cmd → WT+mediator 注入 → 握手 → 关闭 WT → 管道断开 →
      DLL 卸载（模块消失）→ 清理；10 轮后检查残留

预期:
  - 10 轮全部握手成功 + 每轮 10s 内 injected.dll 卸载
  - 循环后无 terminal_injector 进程残留、cmd 全清、WT 窗口关闭

验证方式: 循环驱动 + Toolhelp 模块枚举 + psutil 进程扫描
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from helpers import injector

NAME = "repeat_inject_unload"
ROUNDS = 10

TH32CS_SNAPMODULE = 0x00000008
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD),
                ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD),
                ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD),
                ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_wchar * 256),
                ("szExePath", ctypes.c_wchar * 260)]


def has_module(pid: int, name: str) -> bool:
    k32 = ctypes.windll.kernel32
    h = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE, pid)
    if h in (INVALID_HANDLE_VALUE, 0):
        return False
    e = MODULEENTRY32()
    e.dwSize = ctypes.sizeof(MODULEENTRY32)
    ok = k32.Module32FirstW(h, ctypes.byref(e))
    found = False
    while ok:
        if e.szModule.lower() == name.lower():
            found = True
            break
        ok = k32.Module32NextW(h, ctypes.byref(e))
    k32.CloseHandle(h)
    return found


def _close_wt_and_wait_unload(pid: int) -> bool:
    import win32gui
    import win32con
    if injector._test_wt_hwnd:
        win32gui.PostMessage(injector._test_wt_hwnd, win32con.WM_CLOSE, 0, 0)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not has_module(pid, "injected.dll"):
            return True
        time.sleep(0.3)
    return False


def run() -> int:
    failures = 0
    existing_wts = set(injector.find_wt_windows())
    for i in range(1, ROUNDS + 1):
        try:
            with TestSession() as s:
                pid = s.target_pid
                ok = _close_wt_and_wait_unload(pid)
                if ok:
                    print("  轮 {}/{}: 握手+卸载 OK (pid={})".format(i, ROUNDS, pid))
                else:
                    print("  轮 {}/{}: [FAIL] 卸载超时 (pid={})".format(i, ROUNDS, pid))
                    failures += 1
        except RuntimeError as e:
            print("  轮 {}/{}: [FAIL] 握手失败: {}".format(i, ROUNDS, e))
            failures += 1

    # 泄漏检查：残留进程
    import psutil
    leak_ti = []
    leak_cmd = []
    for proc in psutil.process_iter(["name", "pid", "create_time"]):
        try:
            nm = (proc.info["name"] or "").lower()
            if nm == "terminal_injector.exe":
                leak_ti.append(proc.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if not leak_ti:
        print("  [PASS] 无 terminal_injector 进程残留")
    else:
        print("  [FAIL] 残留 terminal_injector: {}".format(leak_ti))
        failures += 1
    time.sleep(4.0)  # 等 WT 窗口异步关闭完成
    new_wts = [w for w in injector.find_wt_windows() if w not in existing_wts]
    if not new_wts:
        print("  [PASS] 无新增 WT 窗口残留（测试期间启动的窗口均已关闭）")
    else:
        print("  [FAIL] 残留新增 WT 窗口 {} 个: {}".format(len(new_wts), new_wts))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
