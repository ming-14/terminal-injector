"""特性: 管道断开卸载    类别: lifecycle

链路: 关闭 WT 窗口 → mediator 退出 → cmd 与 mediator 管道断开 →
      DllRecvLoop 检测 pipe closed → Unloader → injected.dll 从 cmd
      模块列表消失 → cmd 进程存活（原 ConHost 控制台恢复）

预期（Phase 11）:
  - 关闭 WT 后 10s 内 injected.dll 从 cmd 模块列表消失
  - cmd 进程存活（卸载不崩溃）

验证方式: 驱动 PostMessage(WM_CLOSE) 关 WT + Toolhelp 枚举 cmd 模块
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from helpers import injector

NAME = "unload_clean"

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


def run() -> int:
    failures = 0
    try:
        with TestSession() as s:
            pid = s.target_pid
            if not has_module(pid, "injected.dll"):
                print("  [FAIL] 注入后 injected.dll 不在 cmd 模块列表")
                failures += 1
            else:
                print("  [PASS] 注入后 cmd 模块含 injected.dll")
            # 关闭 WT → mediator 退出 → 管道断开 → DLL 自动卸载
            import win32gui
            import win32con
            if injector._test_wt_hwnd:
                win32gui.PostMessage(injector._test_wt_hwnd, win32con.WM_CLOSE, 0, 0)
                print("  [INFO] 已发送 WM_CLOSE 关闭 WT")
            deadline = time.time() + 10.0
            unloaded = False
            while time.time() < deadline:
                if not has_module(pid, "injected.dll"):
                    unloaded = True
                    break
                time.sleep(0.3)
            if unloaded:
                print("  [PASS] 10s 内 injected.dll 已从 cmd 模块列表消失")
            else:
                print("  [FAIL] 10s 超时 injected.dll 仍在 cmd 模块列表")
                failures += 1
            import psutil
            alive = False
            try:
                alive = psutil.Process(pid).is_running() and \
                    psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                alive = False
            if alive:
                print("  [PASS] cmd 进程存活（卸载后未崩溃，原控制台恢复）")
            else:
                print("  [FAIL] cmd 进程已退出")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
