"""诊断脚本：触发卸载后 attach cdb 查看 DLL 模块/线程状态。

排查 Phase 11 卸载超时问题：日志显示 Unload complete（FreeLibraryAndExitThread
已调用），但 EnumProcessModules 仍能找到 injected.dll。

流程：
  1. 启动 cmd + WT(mediator) + 注入
  2. 等握手
  3. 关闭 WT 窗口触发卸载（mediator stdin EOF → Shutdown → DLL Unloader）
  4. 等待 DLL 卸载（轮询 EnumProcessModules，最多 6 秒）
  5. 若 DLL 仍在，attach cdb 查看模块列表、线程列表、DLL 引用计数

用法：python tests\helpers\diag_unload_debug.py
输出：tests/helpers/cdb_unload_out.txt
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

# 清旧 DLL 日志
for f in glob.glob(r"C:\temp\injected_*.log"):
    try:
        os.remove(f)
    except OSError:
        pass

clear_log()

print("[1/5] 启动目标 cmd...", flush=True)
target_pid = start_target_cmd()
print("  cmd PID = {}".format(target_pid), flush=True)

print("[2/5] 启动 WT + mediator...", flush=True)
mediator_proc = start_wt_mediator(target_pid)

print("[3/5] 等待握手...", flush=True)
if not wait_for_handshake(timeout=20.0):
    print("[FATAL] 握手失败", flush=True)
    sys.exit(1)
print("  握手成功", flush=True)

time.sleep(1.0)


# 关闭 WT 窗口触发卸载
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


# Win32 API 绑定（用于 EnumProcessModules 验证 DLL 是否卸载）
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

_kernel32 = ctypes.windll.kernel32
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL

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

LIST_MODULES_32BIT = 0x01
LIST_MODULES_64BIT = 0x02
LIST_MODULES_ALL = 0x03


def is_dll_loaded(pid: int, name_lower: str) -> bool:
    """检查指定进程是否加载了指定 DLL。"""
    hProc = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not hProc:
        return False
    try:
        cbNeeded = wintypes.DWORD(0)
        if not _psapi.EnumProcessModulesEx(hProc, None, 0, ctypes.byref(cbNeeded),
                                            LIST_MODULES_ALL):
            return False
        count = cbNeeded.value // ctypes.sizeof(wintypes.HMODULE)
        hMods = (wintypes.HMODULE * count)()
        if not _psapi.EnumProcessModulesEx(hProc, hMods, cbNeeded.value,
                                            ctypes.byref(cbNeeded),
                                            LIST_MODULES_ALL):
            return False
        name_buf = ctypes.create_unicode_buffer(260)
        for i in range(count):
            if _psapi.GetModuleFileNameExW(hProc, hMods[i], name_buf, 260) > 0:
                if name_lower in name_buf.value.lower():
                    return True
        return False
    finally:
        _kernel32.CloseHandle(hProc)


print("[4/5] 关闭 WT 窗口触发卸载...", flush=True)
close_wt_window()

# 等 mediator 退出
try:
    mediator_proc.wait(timeout=5.0)
    print("  mediator 已退出", flush=True)
except subprocess.TimeoutExpired:
    print("  mediator 超时未退出，kill", flush=True)
    mediator_proc.kill()

# 轮询 DLL 卸载状态
print("  等待 DLL 卸载...", flush=True)
start = time.time()
deadline = start + 6.0
unloaded = False
while time.time() < deadline:
    if not is_dll_loaded(target_pid, "injected.dll"):
        unloaded = True
        print("  DLL 已卸载（耗时 {:.1f}s）".format(time.time() - start), flush=True)
        break
    time.sleep(0.3)

if unloaded:
    print("[5/5] DLL 卸载成功，无需 attach cdb", flush=True)
    # 清理 cmd
    try:
        import psutil
        p = psutil.Process(target_pid)
        p.terminate()
        p.wait(timeout=3)
    except Exception:
        pass
    sys.exit(0)

print("[5/5] DLL 仍未卸载，attach cdb 查看状态...", flush=True)
print("  cmd PID = {}".format(target_pid), flush=True)

# attach cdb 查看模块、线程、DLL 引用计数
TOOLS = r"c:\Users\rikka\Desktop\terminal-injector\.agents\skills\windows-debugging\10.0.19041.5609"
SYMSRV = "srv*e:\Symbol*http://msdl.blackint3.com:88/download/symbols"
IMGPATH = r"c:\Users\rikka\Desktop\terminal-injector\build\bin\Release"
OUT = r"c:\Users\rikka\Desktop\terminal-injector\tests\helpers\cdb_unload_out.txt"

# cdb 命令：
#   lm          列出所有模块（看 injected.dll 是否在）
#   ~           列出线程
#   ~*kf        全栈（简短，看哪些线程在 injected.dll 代码中）
#   !dlls -c:injected.dll  查看 DLL 引用计数和加载信息
#   qd          分离不杀进程
cmd_str = '"{}" -p {} -y "{};{}" -i "{}" -logo "{}" -c "lm;~;~*kf;!dlls -c:injected.dll;qd"'
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
    print("  cdb stdout:", flush=True)
    print(ret.stdout[-2000:] if ret.stdout else "(empty)", flush=True)
    print("  cdb stderr:", flush=True)
    print(ret.stderr[-1000:] if ret.stderr else "(empty)", flush=True)
except subprocess.TimeoutExpired:
    print("  cdb 超时", flush=True)

# 输出 cdb 日志文件路径
print("\n  cdb 日志已写入: {}".format(OUT), flush=True)
print("  cmd 进程仍在运行，PID={}，可手动 attach 进一步调试".format(target_pid), flush=True)
print("  完成后请手动终止 cmd 进程", flush=True)
