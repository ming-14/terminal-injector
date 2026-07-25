"""诊断脚本：卸载后检查 cmd 的 console 状态 + AttachConsole 失败原因。

排查 Phase 11 验收 2 AttachConsole 失败 err=87 + cmd 退出的根本原因。

err=87 = ERROR_INVALID_PARAMETER
AttachConsole 返回 87 的常见原因：
  1. 调用进程已附加到 console（FreeConsole 失败或未调用）
  2. 目标进程没有 console（被 FreeConsole 过）
  3. 目标进程已退出

流程：
  1. 启动 cmd + WT(mediator) + 注入
  2. 关闭 WT 触发卸载
  3. 等 DLL 卸载完成
  4. 详细检查 cmd console 状态（PEB.ConsoleHandle）
  5. FreeConsole + AttachConsole 复现 err=87
  6. 用 OpenProcess + GetExitCodeProcess 轮询捕获 cmd 退出码

使用方法：
  python tests/helpers/diag_post_unload.py
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

import psutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from helpers import injector
from runners import test_phase11  # 复用其 trigger_unload / find_module_by_name 等
# test_phase11 在 tests/runners/，不在 tests/helpers/


# ============================================================
# Win32 API 绑定
# ============================================================
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
STATUS_SUCCESS = 0

ntdll = ctypes.windll.ntdll
kernel32 = ctypes.windll.kernel32

# NtQueryInformationProcess 查 PEB.ProcessParameters.ConsoleHandle
# PROCESSINFOCLASS: ProcessBasicInformation=0
class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ExitStatus", ctypes.c_void_p),  # NTSTATUS
        ("PebBaseAddress", ctypes.c_void_p),  # PPEB
        ("AffinityMask", ctypes.c_ulong),
        ("BasePriority", ctypes.c_long),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
    ]

ntdll.NtQueryInformationProcess.argtypes = [
    wintypes.HANDLE, ctypes.c_ulong, ctypes.c_void_p,
    wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)
]
ntdll.NtQueryInformationProcess.restype = ctypes.c_long  # NTSTATUS

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.FreeConsole.argtypes = []
kernel32.FreeConsole.restype = wintypes.BOOL
kernel32.AttachConsole.argtypes = [wintypes.DWORD]
kernel32.AttachConsole.restype = wintypes.BOOL
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
kernel32.ReadProcessMemory.restype = wintypes.BOOL


def get_cmd_console_handle(cmd_pid: int):
    """查询 cmd 进程的 PEB.ProcessParameters.ConsoleHandle。

    通过 NtQueryInformationProcess 拿 PEB 地址，再 ReadProcessMemory 读
    PEB.ProcessParameters.ConsoleHandle。

    返回 console 句柄值，或 None（查询失败）。
    """
    hProc = kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, cmd_pid)
    if not hProc:
        print("    OpenProcess 失败 err={}".format(kernel32.GetLastError()))
        return None
    try:
        pbi = PROCESS_BASIC_INFORMATION()
        ret_len = wintypes.ULONG(0)
        status = ntdll.NtQueryInformationProcess(
            hProc, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), ctypes.byref(ret_len))
        if status != STATUS_SUCCESS:
            print("    NtQueryInformationProcess 失败 status={:#x}".format(status))
            return None

        peb = pbi.PebBaseAddress
        if not peb:
            print("    PEB 为空")
            return None

        # PEB 结构（x64）：
        # +0x20 ProcessParameters (RTL_USER_PROCESS_PARAMETERS*)
        # RTL_USER_PROCESS_PARAMETERS：
        # +0x10 ConsoleHandle (HANDLE)
        ptr_size = ctypes.sizeof(ctypes.c_void_p)
        # 读 PEB.ProcessParameters (偏移 0x20 on x64)
        pp_params_addr = peb + 0x20
        buf = ctypes.c_void_p(0)
        bytes_read = ctypes.c_size_t(0)
        if not kernel32.ReadProcessMemory(hProc, pp_params_addr,
                                          ctypes.byref(buf), ptr_size,
                                          ctypes.byref(bytes_read)):
            print("    ReadProcessMemory(PEB.ProcessParameters) 失败 err={}".format(
                kernel32.GetLastError()))
            return None
        params_addr = buf.value
        if not params_addr:
            print("    ProcessParameters 为空")
            return None

        # 读 RTL_USER_PROCESS_PARAMETERS.ConsoleHandle (偏移 0x10 on x64)
        console_handle_addr = params_addr + 0x10
        buf2 = ctypes.c_void_p(0)
        if not kernel32.ReadProcessMemory(hProc, console_handle_addr,
                                          ctypes.byref(buf2), ptr_size,
                                          ctypes.byref(bytes_read)):
            print("    ReadProcessMemory(ConsoleHandle) 失败 err={}".format(
                kernel32.GetLastError()))
            return None
        return buf2.value
    finally:
        kernel32.CloseHandle(hProc)


def check_cmd_state(cmd_pid: int, tag: str) -> bool:
    """检查 cmd 进程状态 + console 句柄。返回 True 表示存活。"""
    print("\n--- {} ---".format(tag))
    try:
        p = psutil.Process(cmd_pid)
        alive = p.is_running()
        status = p.status()
        rss = p.memory_info().rss
        handles = p.num_handles()
        print("  cmd pid={} alive={} status={} rss={:,} handles={}".format(
            cmd_pid, alive, status, rss, handles))
        if not alive:
            return False
    except psutil.NoSuchProcess:
        print("  cmd pid={} DEAD (NoSuchProcess)".format(cmd_pid))
        return False

    # console 窗口
    hwnd = test_phase11.find_console_window_by_pid(cmd_pid)
    print("  console window hwnd={:#x}".format(hwnd or 0))

    # PEB.ConsoleHandle
    console_handle = get_cmd_console_handle(cmd_pid)
    if console_handle is not None:
        print("  PEB.ConsoleHandle = {:#x}".format(console_handle))
        if console_handle == 0 or console_handle == 0xFFFFFFFFFFFFFFFF:
            print("  [警告] ConsoleHandle 为空/无效，cmd 可能没有 console！")
    else:
        print("  PEB.ConsoleHandle 查询失败")

    return True


def test_attach_console_sequence(cmd_pid: int) -> None:
    """复现 AttachConsole 失败 err=87，详细打印每一步。"""
    print("\n=== AttachConsole 失败复现 ===")

    # 1. FreeConsole 前检查测试进程的 console
    test_console = get_cmd_console_handle(os.getpid())
    print("  [测试进程] FreeConsole 前 PEB.ConsoleHandle = {:#x}".format(
        test_console or 0))

    # 2. FreeConsole
    ok = kernel32.FreeConsole()
    err = kernel32.GetLastError() if not ok else 0
    print("  [测试进程] FreeConsole 返回={} err={}".format(ok, err))

    # 3. FreeConsole 后检查
    test_console_after = get_cmd_console_handle(os.getpid())
    print("  [测试进程] FreeConsole 后 PEB.ConsoleHandle = {:#x}".format(
        test_console_after or 0))

    # 4. AttachConsole(cmd_pid)
    ok = kernel32.AttachConsole(cmd_pid)
    err = kernel32.GetLastError() if not ok else 0
    print("  [测试进程] AttachConsole({}) 返回={} err={}".format(cmd_pid, ok, err))

    if not ok:
        print("  [FAIL] AttachConsole 失败 err={}".format(err))
        if err == 87:
            print("    err=87 = ERROR_INVALID_PARAMETER")
            print("    可能原因：测试进程已附加到 console，或 cmd 没有 console")
            if test_console_after:
                print("    测试进程仍有 console，FreeConsole 失败！")
            else:
                print("    测试进程无 console，问题在 cmd 没有 console")
        elif err == 6:
            print("    err=6 = ERROR_INVALID_HANDLE")
            print("    原因：cmd 没有 console")
    else:
        print("  [PASS] AttachConsole 成功")
        # 必须 FreeConsole，否则后续测试受影响
        kernel32.FreeConsole()


def monitor_cmd_exit(cmd_pid: int, duration: float = 5.0) -> None:
    """用 OpenProcess + GetExitCodeProcess 轮询，捕获 cmd 退出码。"""
    print("\n=== 监控 cmd 退出码 ({}s) ===".format(duration))
    STILL_ACTIVE = 259
    hProc = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, cmd_pid)
    if not hProc:
        print("  OpenProcess 失败 err={}".format(kernel32.GetLastError()))
        return
    try:
        start = time.time()
        while time.time() - start < duration:
            exit_code = wintypes.DWORD(0)
            if kernel32.GetExitCodeProcess(hProc, ctypes.byref(exit_code)):
                if exit_code.value != STILL_ACTIVE:
                    print("  cmd 已退出！exit_code={} (0x{:x}) elapsed={:.2f}s".format(
                        exit_code.value, exit_code.value, time.time() - start))
                    return
            time.sleep(0.1)
        print("  cmd 在 {}s 内未退出".format(duration))
    finally:
        kernel32.CloseHandle(hProc)


def main():
    print("=" * 60)
    print("Phase 11 卸载后 cmd 状态诊断")
    print("=" * 60)

    # 1. 启动 cmd
    print("\n[1] 启动 cmd...")
    cmd_pid = injector.start_target_cmd()
    print("    cmd PID={}".format(cmd_pid))

    # 2. 注入
    print("[2] 启动 WT + mediator...")
    injector.clear_log()
    mediator_proc = injector.start_wt_mediator(cmd_pid)
    print("[3] 等待握手...")
    if not injector.wait_for_handshake(timeout=20.0):
        print("[FATAL] 握手失败")
        injector.cleanup(cmd_pid, mediator_proc)
        return 1
    print("    握手成功")
    time.sleep(1.0)

    # 卸载前 cmd 状态
    check_cmd_state(cmd_pid, "卸载前 cmd 状态")

    # 4. 关闭 WT 触发卸载
    print("\n[4] 触发卸载...")
    if not test_phase11.trigger_unload(cmd_pid, mediator_proc, timeout=20.0):
        print("[FAIL] 卸载失败")
        injector.cleanup(cmd_pid, None)
        return 1
    print("    卸载成功")

    # 卸载后立即检查 cmd 状态
    check_cmd_state(cmd_pid, "卸载后立即 cmd 状态")

    # 5. AttachConsole 复现
    test_attach_console_sequence(cmd_pid)

    # 6. SendInput 后监控 cmd 退出
    print("\n[5] SendInput 输入 echo 命令...")
    from helpers import input_sim
    hwnd = test_phase11.find_console_window_by_pid(cmd_pid)
    if hwnd:
        user32 = ctypes.windll.user32
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.5)
        marker = "diag_marker_{}".format(int(time.time()) % 10000)
        input_sim.type_text("echo {}".format(marker))
        time.sleep(0.3)
        input_sim.type_enter()

        # 监控 cmd 退出码
        monitor_cmd_exit(cmd_pid, duration=5.0)
    else:
        print("  [WARN] 未找到 cmd console 窗口")

    # 最终状态
    check_cmd_state(cmd_pid, "SendInput 后 cmd 状态")

    # 清理
    injector.cleanup(cmd_pid, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
