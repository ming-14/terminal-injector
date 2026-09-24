# -*- coding: utf-8 -*-
"""direct_inject.py —— 直接用 CreateRemoteThread+LoadLibraryW 测注入, 不走 mediator。

目的: 用最小变量隔离证明「x64 DLL 注不进 32 位进程」, 而不是靠推断。
对照组:
  1. x64 cmd.exe  (System32)   + x64 DLL  → 期望成功
  2. x86 cmd.exe  (SysWOW64)   + x64 DLL  → 期望失败(位宽不匹配)
  3. python.exe 孙进程(经 py)  + x64 DLL  → 期望成功(证明孙进程本身可注入)
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.VirtualAllocEx.restype = ctypes.c_void_p
k32.VirtualAllocEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                               wintypes.DWORD, wintypes.DWORD]
k32.WriteProcessMemory.restype = wintypes.BOOL
k32.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                   ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
k32.CreateRemoteThread.restype = wintypes.HANDLE
k32.CreateRemoteThread.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.c_void_p]
k32.GetModuleHandleW.restype = wintypes.HMODULE
k32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
k32.GetProcAddress.restype = ctypes.c_void_p
k32.GetProcAddress.argtypes = [wintypes.HMODULE, ctypes.c_char_p]
k32.WaitForSingleObject.restype = wintypes.DWORD
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.GetExitCodeThread.restype = wintypes.BOOL
k32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]

PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT_RESERVE = 0x3000
PAGE_READWRITE = 0x04
SCS = {0: "32BIT", 6: "64BIT"}


def bitness(path):
    bt = ctypes.c_ulong(0)
    if k32.GetBinaryTypeW(ctypes.c_wchar_p(path), ctypes.byref(bt)):
        return SCS.get(bt.value, "?")
    return "?"


def inject(pid, dll_path):
    """最朴素的远程 LoadLibraryW 注入。返回 (成功?, 说明)。"""
    h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        return False, "OpenProcess err={}".format(ctypes.get_last_error())
    try:
        data = ctypes.create_unicode_buffer(dll_path)
        size = ctypes.sizeof(data)
        addr = k32.VirtualAllocEx(h, None, size, MEM_COMMIT_RESERVE, PAGE_READWRITE)
        if not addr:
            return False, "VirtualAllocEx err={}".format(ctypes.get_last_error())
        written = ctypes.c_size_t(0)
        if not k32.WriteProcessMemory(h, addr, ctypes.cast(data, ctypes.c_void_p),
                                      size, ctypes.byref(written)):
            return False, "WriteProcessMemory err={}".format(ctypes.get_last_error())
        p_load = k32.GetProcAddress(k32.GetModuleHandleW("kernel32.dll"),
                                    b"LoadLibraryW")
        if not p_load:
            return False, "GetProcAddress err={}".format(ctypes.get_last_error())
        th = k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(p_load),
                                    ctypes.c_void_p(addr), 0, None)
        if not th:
            return False, "CreateRemoteThread err={}".format(ctypes.get_last_error())
        try:
            k32.WaitForSingleObject(th, 10000)
            code = wintypes.DWORD(0)
            k32.GetExitCodeThread(th, ctypes.byref(code))
            ok = (code.value != 0)
            return ok, "LoadLibraryW exit=0x{:X}".format(code.value)
        finally:
            k32.CloseHandle(th)
    finally:
        k32.CloseHandle(h)


def children_with_ppid(root_pid):
    import psutil
    rows = []
    for c in psutil.Process(root_pid).children(recursive=True):
        rows.append((c.pid, c.ppid(), c.name()))
    return rows


def main():
    dll = os.path.join(
        os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")),
        "build", "bin", "Release", "injected.dll")
    print("目标 DLL: {}  ({})\n".format(os.path.basename(dll), "x64"))

    # ---- 对照组 1/2: 同位数 vs 跨位数
    for label, exe in [("x64 cmd (System32)", r"C:\Windows\System32\cmd.exe"),
                       ("x86 cmd (SysWOW64)", r"C:\Windows\SysWOW64\cmd.exe")]:
        p = subprocess.Popen([exe, "/k", "title probe"],
                             creationflags=subprocess.CREATE_NEW_CONSOLE)
        time.sleep(1.5)
        ok, why = inject(p.pid, dll)
        print("[{}] pid={} bitness={}".format(label, p.pid, bitness(exe)))
        print("     注入 {}: {}  ({})".format("成功" if ok else "失败", why,
                                            ""))
        p.terminate()
        time.sleep(1.0)

    # ---- 对照组 3: py 拉起的孙进程 python.exe
    print()
    launcher = subprocess.Popen(["py.exe", "-c", "import time;time.sleep(30)"],
                                creationflags=subprocess.CREATE_NEW_CONSOLE)
    time.sleep(3.0)

    import psutil
    tree = []
    try:
        for c in psutil.Process(launcher.pid).children(recursive=True):
            tree.append((c.pid, c.ppid(), c.name()))
    except psutil.NoSuchProcess:
        # py.exe 已 re-exec 退出, 直接按命令行找回 python.exe
        for c in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            try:
                cl = " ".join(c.info["cmdline"] or [])
                if "time.sleep(30)" in cl and c.info["name"].lower() == "python.exe":
                    tree.append((c.info["pid"], c.info["ppid"], c.info["name"]))
            except Exception:
                pass

    print("[py 链路] 进程树 (pid, ppid, name):")
    for pid, ppid, name in tree:
        print("     {:>7} {:>7}  {}".format(pid, ppid, name))

    py_pid = next((pid for pid, _, n in tree if n.lower() == "py.exe"), None)
    py_py = next((pid for pid, _, n in tree if n.lower() == "python.exe"), None)

    if py_pid:
        ok, why = inject(py_pid, dll)
        print("  注入 py.exe(pid={}) → {}  ({})".format(py_pid,
              "成功" if ok else "失败", why))
    if py_py:
        ok, why = inject(py_py, dll)
        print("  注入 python.exe(pid={}) → {}  ({})".format(py_py,
              "成功" if ok else "失败", why))
        print("     注: 证明孙进程本身可注入, 问题只在'链断'不在'孙进程'")
    else:
        print("  未找到存活的 python.exe 孙进程")

    try:
        psutil.Process(launcher.pid).terminate()
    except Exception:
        pass
    time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
