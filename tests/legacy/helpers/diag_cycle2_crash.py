"""循环 2 cmd 提前退出问题最小复现脚本。

复现流程：
  1. 启动 cmd
  2. 循环 2 次：
     a. 启动 WT(mediator) 注入 DLL
     b. 等握手
     c. 关闭 WT 窗口触发卸载
     d. 等 DLL 卸载
  3. 第 2 次循环期间监控 cmd 进程退出码

用法：python tests/helpers/diag_cycle2_crash.py
"""
import os
import sys
import time
import ctypes
from ctypes import wintypes

import psutil

sys.path.insert(0, os.path.dirname(__file__))
from injector import (
    start_target_cmd, start_wt_mediator, wait_for_handshake,
    clear_log, find_wt_windows, PROJECT_ROOT,
)

import win32gui
import win32con

# Win32 API 用于获取退出码
_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
STILL_ACTIVE = 0x103


def get_exit_code(pid: int):
    """获取进程退出码。返回 None 表示无法打开进程。"""
    h = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return None
    try:
        code = wintypes.DWORD(0)
        if _kernel32.GetExitCodeProcess(h, ctypes.byref(code)):
            if code.value == STILL_ACTIVE:
                return "STILL_ACTIVE"
            return code.value
        return "GetExitCodeProcess failed err={}".format(ctypes.get_last_error())
    finally:
        _kernel32.CloseHandle(h)


def close_wt_window(hwnd) -> None:
    """关闭 WT 窗口（触发卸载）。"""
    if hwnd and win32gui.IsWindow(hwnd):
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)


def enum_process_modules(pid: int):
    """枚举进程模块，找 injected.dll。"""
    hProc = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not hProc:
        return []
    try:
        import ctypes as ct
        _psapi = ct.windll.psapi
        _psapi.EnumProcessModules.argtypes = [wintypes.HANDLE, ct.c_void_p, wintypes.DWORD, ct.POINTER(wintypes.DWORD)]
        _psapi.EnumProcessModules.restype = wintypes.BOOL
        _psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
        _psapi.GetModuleFileNameExW.restype = wintypes.DWORD
        cb = wintypes.DWORD(0)
        if not _psapi.EnumProcessModules(hProc, None, 0, ct.byref(cb)):
            return []
        count = cb.value // ct.sizeof(wintypes.HMODULE)
        hMods = (wintypes.HMODULE * count)()
        if not _psapi.EnumProcessModules(hProc, hMods, cb.value, ct.byref(cb)):
            return []
        result = []
        buf = ct.create_unicode_buffer(260)
        for i in range(count):
            if _psapi.GetModuleFileNameExW(hProc, hMods[i], buf, 260) > 0:
                result.append(buf.value)
        return result
    finally:
        _kernel32.CloseHandle(hProc)


def get_cmd_exit_code(hCmd):
    """通过预先打开的句柄查 cmd 退出码。进程退出后句柄仍有效。"""
    if not hCmd:
        return None
    code = wintypes.DWORD(0)
    if _kernel32.GetExitCodeProcess(hCmd, ctypes.byref(code)):
        return code.value
    return None


def check_wer_dumps(before_names):
    """检查 WER LocalDumps 是否生成了新 dump。返回新 dump 文件路径列表。"""
    dump_dir = r"C:\temp\cmd_dumps"
    if not os.path.isdir(dump_dir):
        return []
    new_dumps = []
    for name in os.listdir(dump_dir):
        if name not in before_names and name.endswith(".dmp"):
            new_dumps.append(os.path.join(dump_dir, name))
    return new_dumps


def main():
    print("=== 循环 2 cmd 退出问题最小复现（WER 抓 dump，不 attach 调试器）===")
    print("[setup] 启动 cmd...")
    cmd_pid = start_target_cmd()
    print("[setup] cmd PID={}".format(cmd_pid))

    # 打开 cmd 进程句柄用于查退出码（进程退出后句柄仍有效直到我们 CloseHandle）
    hCmd = _kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, cmd_pid)
    print("[setup] hCmd={:#x}".format(hCmd or 0))

    # WER LocalDumps 已配置到 C:\temp\cmd_dumps（通过 wer_localdumps_cmd.reg）
    # cmd.exe 崩溃时 WerFault.exe 会自动抓 full dump 到该目录
    DUMP_DIR = r"C:\temp\cmd_dumps"
    os.makedirs(DUMP_DIR, exist_ok=True)

    try:
        for i in range(2):
            print("\n--- 循环 {} ---".format(i + 1))
            clear_log()
            # 快照已有 dump 文件（用于检测 WER 新生成的 dump）
            dump_before = set(os.listdir(DUMP_DIR)) if os.path.isdir(DUMP_DIR) else set()
            # 启动前快照已有 WT 窗口
            existing = set(find_wt_windows())
            mediator_proc = start_wt_mediator(cmd_pid)
            # 找新增 WT 窗口
            new_hwnds = set(find_wt_windows()) - existing
            wt_hwnd = sorted(new_hwnds)[0] if new_hwnds else None
            print("[cycle {}] wt_hwnd={:#x}".format(i + 1, wt_hwnd or 0))

            if not wait_for_handshake(timeout=15.0):
                print("[cycle {}] 握手失败".format(i + 1))
                return
            print("[cycle {}] 握手成功".format(i + 1))

            # 等待 0.5s 让 DLL 稳定
            time.sleep(0.5)

            # 记录握手后 cmd 状态
            try:
                p = psutil.Process(cmd_pid)
                print("[cycle {}] cmd 状态: alive={} status={} rss={:,} handles={}".format(
                    i + 1, p.is_running(), p.status(), p.memory_info().rss, p.num_handles()))
            except psutil.NoSuchProcess:
                exit_code = get_cmd_exit_code(hCmd)
                print("[cycle {}] cmd 进程消失（握手后）exit_code={:#x}".format(
                    i + 1, exit_code or 0))
                # 检查 WER dump
                new_dumps = check_wer_dumps(dump_before)
                if new_dumps:
                    print("[cycle {}] WER 抓到 dump: {}".format(i + 1, new_dumps))
                return

            # 检查 injected.dll 是否在模块列表
            mods = enum_process_modules(cmd_pid)
            inj = [m for m in mods if "injected.dll" in m.lower()]
            print("[cycle {}] injected.dll in modules: {}".format(i + 1, bool(inj)))

            # 关闭 WT 窗口触发卸载
            print("[cycle {}] 关闭 WT 窗口...".format(i + 1))
            close_wt_window(wt_hwnd)

            # 等 mediator 退出
            try:
                mediator_proc.wait(timeout=5.0)
                print("[cycle {}] mediator 已退出".format(i + 1))
            except Exception as e:
                print("[cycle {}] mediator wait 异常: {}".format(i + 1, e))
                mediator_proc.kill()

            # 等 DLL 卸载（最多 10s），同时高频检查 cmd 是否退出
            print("[cycle {}] 等待 DLL 卸载（高频监控 cmd）...".format(i + 1))
            start = time.time()
            dll_unloaded = False
            cmd_exited = False
            cmd_exit_code = None
            last_check = 0.0
            while time.time() - start < 10.0:
                now = time.time()
                # 0.2s 检查 cmd 是否退出
                if now - last_check > 0.2:
                    last_check = now
                    if not psutil.pid_exists(cmd_pid):
                        cmd_exited = True
                        # 立刻查退出码（用之前打开的句柄）
                        cmd_exit_code = get_cmd_exit_code(hCmd)
                        print("[cycle {}] cmd 在卸载等待期间退出！elapsed={:.2f}s exit_code={:#x}".format(
                            i + 1, now - start, cmd_exit_code or 0))
                        break
                    # 检查 DLL 模块
                    mods = enum_process_modules(cmd_pid)
                    if not any("injected.dll" in m.lower() for m in mods):
                        dll_unloaded = True
                        print("[cycle {}] DLL 已卸载（耗时 {:.2f}s）".format(i + 1, now - start))
                        break

                time.sleep(0.05)

            if cmd_exited:
                print("[cycle {}] 结论：cmd 在循环 {} 退出，退出码={:#x}".format(
                    i + 1, i + 1, cmd_exit_code or 0))
                # 等 WER 写完 dump（WerFault.exe 异步写 dump）
                print("[cycle {}] 等待 WER 写 dump（3s）...".format(i + 1))
                time.sleep(3.0)
                new_dumps = check_wer_dumps(dump_before)
                if new_dumps:
                    print("[cycle {}] WER 抓到 dump:".format(i + 1))
                    for d in new_dumps:
                        print("  {}".format(d))
                else:
                    print("[cycle {}] WER 未抓到 dump（可能退出码不是崩溃，而是主动 ExitProcess）".format(i + 1))
                # 检查 injected.dll 日志
                log_path = r"C:\temp\injected_{}.log".format(cmd_pid)
                if os.path.exists(log_path):
                    size = os.path.getsize(log_path)
                    print("[cycle {}] injected log size: {} bytes".format(i + 1, size))
                    if size > 0:
                        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        print("[cycle {}] injected log 末尾:".format(i + 1))
                        print(content[-2000:] if len(content) > 2000 else content)
                else:
                    print("[cycle {}] injected log 不存在: {}".format(i + 1, log_path))
                return
            if not dll_unloaded:
                print("[cycle {}] DLL 卸载超时".format(i + 1))
                return

            # 卸载后 cmd 状态
            try:
                p = psutil.Process(cmd_pid)
                print("[cycle {}] 卸载后 cmd 状态: alive={} status={} rss={:,} handles={}".format(
                    i + 1, p.is_running(), p.status(), p.memory_info().rss, p.num_handles()))
            except psutil.NoSuchProcess:
                exit_code = get_cmd_exit_code(hCmd)
                print("[cycle {}] 卸载后 cmd 进程消失 exit_code={:#x} (STILL_ACTIVE={:#x})".format(
                    i + 1, exit_code or 0, STILL_ACTIVE))
                # 检查 WER dump
                new_dumps = check_wer_dumps(dump_before)
                if new_dumps:
                    print("[cycle {}] WER 抓到 dump:".format(i + 1))
                    for d in new_dumps:
                        print("  {}".format(d))
                else:
                    print("[cycle {}] WER 未抓到 dump".format(i + 1))
                return

            # 循环间隔
            time.sleep(0.5)

        print("\n=== 两次循环都通过，未复现问题 ===")
    finally:
        if hCmd:
            _kernel32.CloseHandle(hCmd)
        # 清理 cmd
        try:
            p = psutil.Process(cmd_pid)
            for child in p.children(recursive=True):
                try: child.terminate()
                except: pass
            try: p.terminate(); p.wait(timeout=3)
            except: pass
        except: pass


if __name__ == "__main__":
    main()
