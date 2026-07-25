"""增强诊断：触发卸载后用多种独立方法验证 DLL 是否真的还在。

排查 Phase 11 卸载问题：现有 diag_unload_debug.py 只用 EnumProcessModulesEx（PSAPI），
与 cdb 的 !dlls 命令结果矛盾（PSAPI 说在，!dlls 读不到 Ldr Entry）。

本脚本用四种独立方法交叉验证：
  1. EnumProcessModulesEx（PSAPI，与现有脚本一致）
  2. CreateToolhelp32Snapshot（Toolhelp32，独立于 PSAPI）
  3. ReadProcessMemory 读 DLL 基址的 "MZ" 头（看内存是否释放）
  4. cdb 用 !peb + 手动遍历 Ldr 链表（获取 LoadCount）

触发卸载后立即检查 + 等待 30 秒持续检查，看 DLL 是否最终消失。

用法：python tests\helpers\diag_dll_unload_thorough.py
输出：tests/helpers/cdb_thorough_out.txt
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

# ============================================================
# Win32 API 绑定
# ============================================================
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.ReadProcessMemory.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
]
_kernel32.ReadProcessMemory.restype = wintypes.BOOL
_kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_kernel32.Process32FirstW.restype = wintypes.BOOL
_kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_kernel32.Process32NextW.restype = wintypes.BOOL
_kernel32.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_kernel32.Module32FirstW.restype = wintypes.BOOL
_kernel32.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
_kernel32.Module32NextW.restype = wintypes.BOOL

_psapi = ctypes.windll.psapi
_psapi.EnumProcessModulesEx.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD), wintypes.DWORD
]
_psapi.EnumProcessModulesEx.restype = wintypes.BOOL
_psapi.GetModuleFileNameExW.argtypes = [
    wintypes.HANDLE, wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD
]
_psapi.GetModuleFileNameExW.restype = wintypes.DWORD

# Toolhelp32 常量
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
LIST_MODULES_ALL = 0x03
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class MODULEENTRY32W(ctypes.Structure):
    """Toolhelp32 模块条目（Unicode）。"""
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


# ============================================================
# 检查方法 1：EnumProcessModulesEx（PSAPI）
# ============================================================
def check_psapi(pid: int):
    """返回 (found, base_addr, path) 或 (False, 0, None)。"""
    hProc = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not hProc:
        return (False, 0, None)
    try:
        cbNeeded = wintypes.DWORD(0)
        if not _psapi.EnumProcessModulesEx(hProc, None, 0, ctypes.byref(cbNeeded),
                                            LIST_MODULES_ALL):
            return (False, 0, None)
        count = cbNeeded.value // ctypes.sizeof(wintypes.HMODULE)
        hMods = (wintypes.HMODULE * count)()
        if not _psapi.EnumProcessModulesEx(hProc, hMods, cbNeeded.value,
                                            ctypes.byref(cbNeeded),
                                            LIST_MODULES_ALL):
            return (False, 0, None)
        name_buf = ctypes.create_unicode_buffer(260)
        for i in range(count):
            if _psapi.GetModuleFileNameExW(hProc, hMods[i], name_buf, 260) > 0:
                if "injected.dll" in name_buf.value.lower():
                    return (True, int(hMods[i]) or 0, name_buf.value)
        return (False, 0, None)
    finally:
        _kernel32.CloseHandle(hProc)


# ============================================================
# 检查方法 2：CreateToolhelp32Snapshot（Toolhelp32，独立于 PSAPI）
# ============================================================
def check_toolhelp(pid: int):
    """返回 (found, base_addr, path) 或 (False, 0, None)。"""
    snap = _kernel32.CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return (False, 0, None)
    try:
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(MODULEENTRY32W)
        if not _kernel32.Module32FirstW(snap, ctypes.byref(me)):
            return (False, 0, None)
        while True:
            if "injected.dll" in me.szModule.lower():
                return (True, int(me.modBaseAddr) or 0, me.szExePath)
            if not _kernel32.Module32NextW(snap, ctypes.byref(me)):
                break
        return (False, 0, None)
    finally:
        _kernel32.CloseHandle(snap)


# ============================================================
# 检查方法 3：ReadProcessMemory 读 DLL 基址看 "MZ" 头
# ============================================================
def check_memory_mz(pid: int, base_addr: int):
    """读取 base_addr 处 2 字节，看是否为 "MZ"（DLL 内存未释放）。
    返回 (readable, is_mz, first_bytes_hex)。
    """
    if not base_addr:
        return (False, False, "")
    hProc = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not hProc:
        return (False, False, "")
    try:
        buf = (ctypes.c_ubyte * 64)()
        bytesRead = ctypes.c_size_t(0)
        ok = _kernel32.ReadProcessMemory(
            hProc, ctypes.c_void_p(base_addr), buf, 64,
            ctypes.byref(bytesRead))
        if not ok or bytesRead.value < 2:
            return (False, False, "")
        hex_str = " ".join("{:02x}".format(buf[i]) for i in range(min(16, bytesRead.value)))
        is_mz = (buf[0] == 0x4D and buf[1] == 0x5A)  # 'M' 'Z'
        return (True, is_mz, hex_str)
    finally:
        _kernel32.CloseHandle(hProc)


# ============================================================
# 触发卸载
# ============================================================
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


# ============================================================
# 主流程
# ============================================================
# 清旧 DLL 日志
for f in glob.glob(r"C:\temp\injected_*.log"):
    try:
        os.remove(f)
    except OSError:
        pass

clear_log()

print("[1/4] 启动目标 cmd...", flush=True)
target_pid = start_target_cmd()
print("  cmd PID = {}".format(target_pid), flush=True)

print("[2/4] 启动 WT + mediator...", flush=True)
mediator_proc = start_wt_mediator(target_pid)

print("[3/4] 等待握手...", flush=True)
if not wait_for_handshake(timeout=20.0):
    print("[FATAL] 握手失败", flush=True)
    sys.exit(1)
print("  握手成功", flush=True)

time.sleep(1.0)

# 卸载前先记录 DLL 基址（用 PSAPI）
print("\n[pre-unload] 卸载前 DLL 状态检查...", flush=True)
found, base_pre, path_pre = check_psapi(target_pid)
print("  PSAPI: found={} base={:#x} path={}".format(
    found, base_pre, path_pre), flush=True)
if not found:
    print("[FATAL] 卸载前 DLL 不在模块列表，无法测试", flush=True)
    sys.exit(1)

found_th, base_th, path_th = check_toolhelp(target_pid)
print("  Toolhelp: found={} base={:#x}".format(
    found_th, base_th), flush=True)

readable, is_mz, hex_str = check_memory_mz(target_pid, base_pre)
print("  Memory@base: readable={} is_mz={} bytes={}".format(
    readable, is_mz, hex_str), flush=True)

print("\n[4/4] 关闭 WT 窗口触发卸载...", flush=True)
close_wt_window()

# 等 mediator 退出
try:
    mediator_proc.wait(timeout=5.0)
    print("  mediator 已退出", flush=True)
except subprocess.TimeoutExpired:
    print("  mediator 超时未退出，kill", flush=True)
    mediator_proc.kill()

# 持续轮询 30 秒，每 2 秒检查一次
print("\n[post-unload] 持续检查 30 秒...", flush=True)
start = time.time()
last_print = 0
unloaded = False
while time.time() - start < 30.0:
    elapsed = time.time() - start

    # 每种方法都检查
    found_psapi, base_psapi, _ = check_psapi(target_pid)
    found_th, base_th, _ = check_toolhelp(target_pid)

    # 如果 PSAPI 找到，读内存看 MZ
    if found_psapi and base_psapi:
        readable, is_mz, hex_str = check_memory_mz(target_pid, base_psapi)
    else:
        readable, is_mz, hex_str = (False, False, "")

    # 每 2 秒打印一次状态
    if elapsed - last_print >= 2.0:
        print("  [{:5.1f}s] PSAPI={} Toolhelp={} mem_read={} mz={} bytes={}".format(
            elapsed, found_psapi, found_th, readable, is_mz, hex_str), flush=True)
        last_print = elapsed

    # 三种方法都说不在，才算卸载
    if not found_psapi and not found_th:
        print("  [{:5.1f}s] DLL 已卸载（PSAPI + Toolhelp 都确认）".format(elapsed), flush=True)
        unloaded = True
        break

    time.sleep(0.5)

# 最终状态
print("\n[final] 最终状态（{}）:".format(
    "已卸载" if unloaded else "仍未卸载"), flush=True)
found_psapi, base_psapi, path_psapi = check_psapi(target_pid)
found_th, base_th, path_th = check_toolhelp(target_pid)
print("  PSAPI:    found={} base={:#x}".format(found_psapi, base_psapi), flush=True)
print("  Toolhelp: found={} base={:#x}".format(found_th, base_th), flush=True)

if found_psapi and base_psapi:
    readable, is_mz, hex_str = check_memory_mz(target_pid, base_psapi)
    print("  Memory@base: readable={} is_mz={} bytes={}".format(
        readable, is_mz, hex_str), flush=True)

# 读 DLL 日志看 DoUnload 是否完成
log_path = r"C:\temp\injected_{}.log".format(target_pid)
if os.path.exists(log_path):
    print("\n[DLL log] 末尾 20 行:", flush=True)
    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
        for line in lines[-20:]:
            print("  " + line.rstrip(), flush=True)

# 如果仍未卸载，attach cdb 做深度检查
if not unloaded:
    print("\n[deep-debug] DLL 仍未卸载，attach cdb 做深度检查...", flush=True)
    TOOLS = r"c:\Users\rikka\Desktop\terminal-injector\.agents\skills\windows-debugging\10.0.19041.5609"
    SYMSRV = "srv*e:\Symbol*http://msdl.blackint3.com:88/download/symbols"
    IMGPATH = r"c:\Users\rikka\Desktop\terminal-injector\build\bin\Release"
    OUT = r"c:\Users\rikka\Desktop\terminal-injector\tests\helpers\cdb_thorough_out.txt"

    # cdb 命令：
    #   !peb                     显示 PEB（含 Ldr 指针）
    #   dt ntdll!_PEB_LDR_DATA   显示 Ldr 数据结构
    #   lm                       列模块
    #   ~                        列线程
    #   ~*kf                     全栈
    #   !dlls                    全部 DLL
    #   qd                       分离
    cmd_str = ('"{}" -p {} -y "{};{}" -i "{}" -logo "{}" '
               '-c "!peb;lm;~;~*kf;!dlls;qd"')
    cmd_str = cmd_str.format(
        os.path.join(TOOLS, "cdb.exe"),
        target_pid,
        SYMSRV, IMGPATH,
        IMGPATH,
        OUT,
    )
    print("  运行 cdb: {}".format(cmd_str), flush=True)
    try:
        ret = subprocess.run(cmd_str, shell=True, capture_output=True,
                             text=True, timeout=60)
        print("  cdb exit code: {}".format(ret.returncode), flush=True)
        print("  cdb stdout tail:", flush=True)
        print(ret.stdout[-1500:] if ret.stdout else "(empty)", flush=True)
    except subprocess.TimeoutExpired:
        print("  cdb 超时", flush=True)

    print("\n  cdb 日志: {}".format(OUT), flush=True)
    print("  cmd 进程仍在运行 PID={}".format(target_pid), flush=True)

    # 检查 cdb 输出中是否有 injected.dll 的 Ldr 信息
    if os.path.exists(OUT):
        with open(OUT, "r", encoding="utf-8", errors="replace") as f:
            cdb_out = f.read()
        # 找 injected 相关行
        print("\n  [cdb 输出中 injected 相关行]:", flush=True)
        for line in cdb_out.splitlines():
            if "injected" in line.lower():
                print("    " + line.rstrip(), flush=True)

# 清理
print("\n[teardown] 终止 cmd...", flush=True)
try:
    import psutil
    p = psutil.Process(target_pid)
    for child in p.children(recursive=True):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    p.terminate()
    p.wait(timeout=3)
except Exception:
    pass
print("完成", flush=True)
