# -*- coding: utf-8 -*-
"""inject_suspended.py -- 验证承重假设: 现有 --inject 能否注入【挂起中】的 64 位进程

为什么要问:
  方案落地后, 孙进程 (64 位) 由 32 位中继创建为 CREATE_SUSPENDED, mediator fork
  出的注入器要对这个"还挂着的"进程做注入。若这条路不通, 整个复用就站不住。

已知坑: 挂起进程的 loader 未运行, PEB 模块链表为空 -> EnumProcessModulesEx
        返回 err=299。所以要分别确认 [注入前枚举为空] 与 [注入后能枚举到 DLL]。

判据:
  1. 挂起态枚举 -> needed=0 (证明它确实没被 loader 初始化)
  2. terminal_injector.exe --inject 退出码 0
  3. 注入后枚举 -> 含 injected.dll
  4. ResumeThread 后 gc64.exe 正常跑完
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
P32 = os.path.join(HERE, "probe32")
GC64 = os.path.join(P32, "gc64.exe")
BIN = os.path.join(os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
    os.path.join(HERE, "..", "..")), "build", "bin", "Release")
EXE = os.path.join(BIN, "terminal_injector.exe")
INJ = os.path.join(BIN, "injected.dll")
VERDICT = os.path.join(P32, "gc_verdict.txt")
GCDONE = os.path.join(P32, "gc_done.txt")
LOADED64 = os.path.join(P32, "loaded64.txt")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                               ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                               ctypes.c_void_p]
k32.ResumeThread.argtypes = [wintypes.HANDLE]
k32.CloseHandle.argtypes = [wintypes.HANDLE]

psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.LPWSTR, wintypes.DWORD]

CREATE_SUSPENDED = 0x00000004
LIST_MODULES_64BIT = 0x02
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -- " + detail) if detail else ""))


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


def enum_modules(pid, flag=LIST_MODULES_64BIT):
    h = k32.OpenProcess(0x0410, False, pid)
    if not h:
        return [], -1
    try:
        need = wintypes.DWORD(0)
        r = psapi.EnumProcessModulesEx(h, None, 0, ctypes.byref(need), flag)
        err = ctypes.get_last_error()
        if not r or not need.value:
            return [], err
        buf = ctypes.create_string_buffer(need.value)
        if not psapi.EnumProcessModulesEx(h, buf, need.value, ctypes.byref(need), flag):
            return [], ctypes.get_last_error()
        out = []
        step = ctypes.sizeof(ctypes.c_void_p)
        for i in range(need.value // step):
            base = ctypes.c_void_p.from_buffer(buf, i * step).value
            nm = ctypes.create_unicode_buffer(512)
            if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(base), nm, 512):
                out.append(os.path.basename(nm.value).lower())
        return out, 0
    finally:
        k32.CloseHandle(h)


def main():
    for p in (VERDICT, GCDONE, LOADED64):
        try:
            os.remove(p)
        except OSError:
            pass

    print("=== 1. 以 CREATE_SUSPENDED 启动 64 位目标 ===")
    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(si)
    pi = PROCESS_INFORMATION()
    if not k32.CreateProcessW(ctypes.c_wchar_p(GC64), None, None, None, False,
                              CREATE_SUSPENDED, None, ctypes.c_wchar_p(P32),
                              ctypes.byref(si), ctypes.byref(pi)):
        print("CreateProcessW 失败 err=%d" % ctypes.get_last_error())
        return 1
    pid = pi.dwProcessId
    print("  目标 pid = %d (已挂起, 主线程未跑)" % pid)

    mods, err = enum_modules(pid)
    print("  挂起态模块数 = %d, err = %d" % (len(mods), err))
    check("挂起进程 loader 未初始化 (模块表为空, err=299 ERROR_PARTIAL_COPY)",
          len(mods) == 0 and err == 299, "count=%d err=%d" % (len(mods), err))

    print("\n=== 2. 对【挂起中】的目标跑现有 --inject ===")
    t0 = time.time()
    cp = subprocess.run([EXE, "--inject", str(pid), "--dll", INJ],
                        capture_output=True, timeout=60)
    dt = (time.time() - t0) * 1000
    out = (cp.stdout.decode("utf-8", "replace") +
           cp.stderr.decode("utf-8", "replace")).strip()
    print("  退出码 = %d, 耗时 %.0f ms" % (cp.returncode, dt))
    if out:
        print("  输出: " + out.replace("\n", "\n        "))
    check("--inject 退出码为 0", cp.returncode == 0, "rc=%d" % cp.returncode)

    time.sleep(0.5)
    mods2, err2 = enum_modules(pid)
    has = any("injected.dll" in m for m in mods2)
    print("  注入后模块数 = %d, 含 injected.dll = %s" % (len(mods2), has))
    check("注入后目标模块表含 injected.dll", has, "err=%d" % err2)

    print("\n=== 3. ResumeThread, 目标应正常跑完 ===")
    k32.ResumeThread(pi.hThread)
    for _ in range(100):
        if os.path.exists(GCDONE):
            break
        time.sleep(0.1)
    ok = os.path.exists(GCDONE)
    check("恢复后目标正常跑完(gc_done.txt)", ok)
    check("恢复后目标未被注入破坏(gc_verdict.txt 生成)", os.path.exists(VERDICT))
    if os.path.exists(VERDICT):
        print("  gc_verdict: " + open(VERDICT).read().strip())

    k32.CloseHandle(pi.hThread)
    k32.CloseHandle(pi.hProcess)

    print("\n=== 汇总 ===")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        print("  %-4s %s" % ("PASS" if ok else "FAIL", name))
    print("\n%d/%d 项通过" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
