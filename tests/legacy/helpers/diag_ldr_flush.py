"""Phase 11 诊断：触发 LDR flush 验证 DLL 能否从模块列表消失。

背景：
  卸载后 LoadCount=1，State=9（LdrModulesReadyToUnload），DETACH 已调用。
  这是 Windows LDR 延迟卸载机制：DLL 已"逻辑卸载"（DETACH 调用，Hook 卸载，
  资源释放），但"物理未卸载"（模块仍在 LdrLists）。

  LDR 在下次 Loader 操作（LoadLibrary/FreeLibrary/GetModuleHandle 等）时会
  调用 LdrpFlushUnloadCompleteProcessing 清理 LdrpUnloadNodeList，真正释放
  模块内存。

测试方法：
  1. 启动 cmd + WT，注入 DLL
  2. 关闭 WT 触发卸载
  3. 等 DoUnload 完成
  4. 检查卸载前 LoadCount（应=1）
  5. 用 CreateRemoteThread 让 cmd 进程调 LoadLibraryW("kernel32.dll")
     触发 LDR flush
  6. 等待远程线程完成
  7. 检查 injected.dll 是否从模块列表消失（LoadCount=0 或模块不存在）

不杀 cmd 进程（保留现场）。不影响其他 WT 进程。

用法：python tests\helpers\diag_ldr_flush.py
"""
import ctypes
import glob
import os
import subprocess
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(__file__))
from injector import (  # noqa: E402
    clear_log,
    start_target_cmd,
    start_wt_mediator,
    wait_for_handshake,
)
from diag_peb_loadcount import find_injected_loadcount  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402


# ============================================================
# Win32 API 绑定（用于 CreateRemoteThread 触发 LDR flush）
# ============================================================
PROCESS_CREATE_THREAD = 0x0002
PROCESS_VM_OPERATION = 0x0008
PROCESS_VM_WRITE = 0x0020
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.VirtualAllocEx.restype = wintypes.LPVOID
_kernel32.VirtualAllocEx.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t,
    wintypes.DWORD, wintypes.DWORD,
]
_kernel32.VirtualFreeEx.restype = wintypes.BOOL
_kernel32.VirtualFreeEx.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, ctypes.c_size_t, wintypes.DWORD,
]
_kernel32.WriteProcessMemory.restype = wintypes.BOOL
_kernel32.WriteProcessMemory.argtypes = [
    wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCVOID,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetProcAddress.restype = ctypes.c_void_p
_kernel32.GetProcAddress.argtypes = [wintypes.HMODULE, wintypes.LPCSTR]
_kernel32.CreateRemoteThread.restype = wintypes.HANDLE
# SECURITY_ATTRIBUTES 在 Python 3.8 wintypes 未定义，用 c_void_p 代替（传 None 即可）
_kernel32.CreateRemoteThread.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.c_void_p, wintypes.LPVOID,
    wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.GetExitCodeThread.restype = wintypes.BOOL
_kernel32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_READWRITE = 0x04
INFINITE = 0xFFFFFFFF


def trigger_ldr_flush(target_pid, dll_to_load="kernel32.dll"):
    """用 CreateRemoteThread 让目标进程调 LoadLibraryW，触发 LDR flush。

    LoadLibraryW 会调用 LdrLoadDll，内部触发
    LdrpFlushUnloadCompleteProcessing 清理待卸载模块。

    返回 (成功?, LoadLibrary 返回的 HMODULE 或 错误信息)。
    """
    access = (PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION |
              PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)
    hProc = _kernel32.OpenProcess(access, False, target_pid)
    if not hProc:
        return False, "OpenProcess failed err={}".format(ctypes.get_last_error())

    try:
        # 准备 DLL 路径字符串（含 null 终止符）
        dll_path = dll_to_load + "\0"
        path_bytes = dll_path.encode("utf-16-le")
        path_bytes_len = len(path_bytes)

        # 在目标进程分配内存存放路径
        remote_str = _kernel32.VirtualAllocEx(
            hProc, None, path_bytes_len, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE)
        if not remote_str:
            return False, "VirtualAllocEx failed err={}".format(ctypes.get_last_error())

        try:
            # 写入路径
            written = ctypes.c_size_t(0)
            if not _kernel32.WriteProcessMemory(
                    hProc, remote_str, dll_path, path_bytes_len, ctypes.byref(written)):
                return False, "WriteProcessMemory failed err={}".format(ctypes.get_last_error())

            # 获取 LoadLibraryW 地址（x64 下 kernel32 在所有进程加载地址相同）
            hK32 = _kernel32.GetModuleHandleW("kernel32.dll")
            if not hK32:
                return False, "GetModuleHandleW(kernel32) failed"
            pLoadLib = _kernel32.GetProcAddress(hK32, b"LoadLibraryW")
            if not pLoadLib:
                return False, "GetProcAddress(LoadLibraryW) failed"

            # 创建远程线程调用 LoadLibraryW(remote_str)
            hThread = _kernel32.CreateRemoteThread(
                hProc, None, 0, pLoadLib, remote_str, 0, None)
            if not hThread:
                return False, "CreateRemoteThread failed err={}".format(ctypes.get_last_error())

            try:
                # 等待线程完成（10s 超时）
                wait_res = _kernel32.WaitForSingleObject(hThread, 10000)
                if wait_res != 0:  # WAIT_OBJECT_0 = 0
                    return False, "WaitForSingleObject res={} err={}".format(
                        wait_res, ctypes.get_last_error())

                # 获取线程退出码（即 LoadLibraryW 返回值 HMODULE）
                exit_code = wintypes.DWORD(0)
                if not _kernel32.GetExitCodeThread(hThread, ctypes.byref(exit_code)):
                    return False, "GetExitCodeThread failed err={}".format(
                        ctypes.get_last_error())

                return True, "LoadLibraryW returned HMODULE=0x{:x}".format(exit_code.value)
            finally:
                _kernel32.CloseHandle(hThread)
        finally:
            _kernel32.VirtualFreeEx(hProc, remote_str, 0, MEM_RELEASE)
    finally:
        _kernel32.CloseHandle(hProc)


def trigger_free_library(target_pid, hmodule):
    """用 CreateRemoteThread 让目标进程调 FreeLibrary(hmodule)，减少 LoadCount。

    测试 LoadCount=1 是否可通过远程 FreeLibrary 减到 0。
    返回 (成功?, FreeLibrary 返回值 或 错误信息)。
    """
    access = (PROCESS_CREATE_THREAD | PROCESS_VM_OPERATION |
              PROCESS_VM_WRITE | PROCESS_QUERY_INFORMATION | PROCESS_VM_READ)
    hProc = _kernel32.OpenProcess(access, False, target_pid)
    if not hProc:
        return False, "OpenProcess failed err={}".format(ctypes.get_last_error())

    try:
        # 获取 FreeLibrary 地址
        hK32 = _kernel32.GetModuleHandleW("kernel32.dll")
        if not hK32:
            return False, "GetModuleHandleW(kernel32) failed"
        pFreeLib = _kernel32.GetProcAddress(hK32, b"FreeLibrary")
        if not pFreeLib:
            return False, "GetProcAddress(FreeLibrary) failed"

        # FreeLibrary(HMODULE) 参数是 HMODULE（指针大小）
        # CreateRemoteThread 的 lpParameter 是 PVOID，直接传 HMODULE 值
        hThread = _kernel32.CreateRemoteThread(
            hProc, None, 0, pFreeLib, hmodule, 0, None)
        if not hThread:
            return False, "CreateRemoteThread failed err={}".format(ctypes.get_last_error())

        try:
            wait_res = _kernel32.WaitForSingleObject(hThread, 10000)
            if wait_res != 0:
                return False, "WaitForSingleObject res={} err={}".format(
                    wait_res, ctypes.get_last_error())

            exit_code = wintypes.DWORD(0)
            if not _kernel32.GetExitCodeThread(hThread, ctypes.byref(exit_code)):
                return False, "GetExitCodeThread failed err={}".format(
                    ctypes.get_last_error())

            # FreeLibrary 返回 BOOL（非 0 表示成功）
            return True, "FreeLibrary returned {} (HMODULE=0x{:x})".format(
                exit_code.value, hmodule)
        finally:
            _kernel32.CloseHandle(hThread)
    finally:
        _kernel32.CloseHandle(hProc)


def close_wt_window():
    try:
        import win32gui
        import win32con
    except ImportError:
        return False
    import injector as inj
    hwnd = inj._test_wt_hwnd
    if hwnd is None or not win32gui.IsWindow(hwnd):
        return False
    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    inj._test_wt_hwnd = None
    return True


def main():
    # 清旧 DLL 日志
    for f in glob.glob(paths.injected_log_glob()):
        try:
            os.remove(f)
        except OSError:
            pass

    clear_log()

    print("[1/7] 启动目标 cmd...", flush=True)
    target_pid = start_target_cmd()
    print("  cmd PID = {}".format(target_pid), flush=True)

    print("[2/7] 启动 WT + mediator...", flush=True)
    mediator_proc = start_wt_mediator(target_pid)

    print("[3/7] 等待握手...", flush=True)
    if not wait_for_handshake(timeout=20.0):
        print("[FATAL] 握手失败", flush=True)
        sys.exit(1)
    print("  握手成功", flush=True)

    time.sleep(1.0)

    print("\n[4/7] 关闭 WT 窗口触发卸载...", flush=True)
    close_wt_window()

    try:
        mediator_proc.wait(timeout=5.0)
        print("  mediator 已退出", flush=True)
    except subprocess.TimeoutExpired:
        print("  mediator 超时未退出，kill", flush=True)
        mediator_proc.kill()

    # 等 DoUnload 完成（Logger::Shutdown 前最后一行日志）
    dll_log = paths.injected_log(target_pid)
    print("\n[wait] 等待 DoUnload 完成（shutting down logger 日志）...", flush=True)
    deadline = time.time() + 15.0
    unloaded_log_seen = False
    while time.time() < deadline:
        if os.path.exists(dll_log):
            with open(dll_log, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
                if "shutting down logger" in content:
                    unloaded_log_seen = True
                    break
        time.sleep(0.3)

    if unloaded_log_seen:
        print("  DoUnload 已调用 Logger::Shutdown", flush=True)
    else:
        print("  超时未看到 shutting down logger 日志", flush=True)

    # 等卸载线程退出
    time.sleep(2.0)

    print("\n[5/7] 卸载后（flush 前）检查 LoadCount...", flush=True)
    info = find_injected_loadcount(target_pid, dump_bytes=False)
    if info:
        print("  injected.dll: LoadCount={} flags={:#x}".format(
            info["loadcount"], info["flags"]), flush=True)
    else:
        print("  injected.dll 未在模块列表（已物理卸载？）", flush=True)
        print("\n[done] DLL 已物理卸载，无需 flush", flush=True)
        return

    print("\n[6/7] 触发 LDR flush（远程 LoadLibraryW(\"kernel32.dll\")）...", flush=True)
    ok, msg = trigger_ldr_flush(target_pid, "kernel32.dll")
    print("  结果: ok={} msg={}".format(ok, msg), flush=True)

    # 等待 LDR flush 完成
    time.sleep(1.0)

    print("\n[7/7] flush 后检查 injected.dll 是否从模块列表消失...", flush=True)
    info_after = find_injected_loadcount(target_pid, dump_bytes=False)
    if info_after is None:
        print("  *** injected.dll 已从模块列表消失！LDR flush 成功 ***", flush=True)
        print("\n[结论] LoadCount=1 是 LDR 延迟卸载状态，触发 Loader 操作后 DLL 真正卸载", flush=True)
    else:
        print("  injected.dll 仍在模块列表", flush=True)
        print("  LoadCount={} flags={:#x}".format(
            info_after["loadcount"], info_after["flags"]), flush=True)
        if info_after["loadcount"] == 0:
            print("  LoadCount=0 但模块未释放，可能 LDR flush 未触发", flush=True)
        else:
            print("  LoadCount={} 仍非 0，LDR flush 未清理".format(info_after["loadcount"]),
                  flush=True)

    # 再尝试一次 LoadLibrary + FreeLibrary（更彻底的 flush）
    print("\n[额外] 再尝试 LoadLibraryW + FreeLibrary 组合（更彻底 flush）...", flush=True)
    ok2, msg2 = trigger_ldr_flush(target_pid, "ntdll.dll")
    print("  LoadLibrary(ntdll.dll): ok={} msg={}".format(ok2, msg2), flush=True)
    time.sleep(1.0)

    info_final = find_injected_loadcount(target_pid, dump_bytes=False)
    if info_final is None:
        print("  *** injected.dll 已从模块列表消失 ***", flush=True)
    else:
        print("  injected.dll 仍在：LoadCount={} flags={:#x}".format(
            info_final["loadcount"], info_final["flags"]), flush=True)

    # 终极测试：远程 FreeLibrary(injected.dll) 强制减少 LoadCount
    # 测试 LoadCount=1 是否可通过外部 FreeLibrary 减到 0
    print("\n[终极] 远程 FreeLibrary(injected.dll) 强制减少 LoadCount...", flush=True)
    # 获取 injected.dll 的 HMODULE（base address）
    info_for_hmod = find_injected_loadcount(target_pid, dump_bytes=False)
    if info_for_hmod and info_for_hmod["dll_base"]:
        hmod = info_for_hmod["dll_base"]
        print("  injected.dll HMODULE = 0x{:x}".format(hmod), flush=True)
        ok3, msg3 = trigger_free_library(target_pid, hmod)
        print("  FreeLibrary: ok={} msg={}".format(ok3, msg3), flush=True)
        time.sleep(1.0)

        info_post_freelib = find_injected_loadcount(target_pid, dump_bytes=False)
        if info_post_freelib is None:
            print("  *** injected.dll 已从模块列表消失！远程 FreeLibrary 成功 ***", flush=True)
            print("\n[结论] LoadCount=1 是真正的引用计数，远程 FreeLibrary 可减到 0 触发卸载", flush=True)
        else:
            print("  injected.dll 仍在：LoadCount={} flags={:#x}".format(
                info_post_freelib["loadcount"], info_post_freelib["flags"]), flush=True)
    else:
        print("  injected.dll 未在模块列表，跳过 FreeLibrary 测试", flush=True)

    # 读 DLL 日志末尾
    if os.path.exists(dll_log):
        print("\n[DLL log] 末尾 10 行:", flush=True)
        with open(dll_log, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            for line in lines[-10:]:
                print("  " + line.rstrip(), flush=True)

    print("\n[done] cmd 进程 PID={} 仍在运行，供进一步调试".format(target_pid), flush=True)
    print("  手动清理: taskkill /F /PID {}".format(target_pid), flush=True)


if __name__ == "__main__":
    main()
