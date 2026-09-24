# -*- coding: utf-8 -*-
"""relay_bootstrap_probe.py -- 判定: 跨位数向【挂起的 32 位子进程】注入 32 位中继
到底可行不可行, 走哪条路。

背景(text 结论):
  pwsh(64) --CreateProcess--> py.exe(32) --CreateProcess--> python(64)
  x64 的 injected.dll 必须把 relay32.dll 送进【被冻住的】py.exe。
  但 injected.dll 是 64 位进程, 本地 kernel32!LoadLibraryW 是 64 位地址,
  在 32 位目标里无意义。同位数"KnownDLL 同址"技巧在此失效。

  同位数为什么能用: 挂起进程里 kernel32 虽然还没映射, 但 KnownDLL 保证它会
  被映射到【固定基址】, 而我们传入的正是那个固定基址; 远程线程启动会触发
  loader 初始化, 等它执行到入口时 kernel32 已在位。

  那么 32 位目标需要的是一个【32 位注入器】进程。本探针要确认:
    (A) 32 位 KnownDLL 基址是否在同一 boot 内跨进程一致  -> 前提
    (B) 挂起的 WOW64 进程里到底映射了哪些映像
    (C) 【核心】32 位进程能否向挂起的 32 位进程注入成功
    (D) 对照: 32 位进程向运行中的 32 位进程注入 (证明工具本身没问题)
    (E) 64 位侧向挂起的 32 位进程注入 64 位 DLL 会怎样 (路线分歧点)

用法: python relay_bootstrap_probe.py
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
P32 = os.path.join(HERE, "probe32")
TARGET32 = os.path.join(P32, "target32.exe")
INJECT32 = os.path.join(P32, "inject32.exe")
PROBE32 = os.path.join(P32, "probe32.dll")
PROBE64 = os.path.join(P32, "probe64.dll")
MARK32 = os.path.join(P32, "loaded32.txt")
MARK64 = os.path.join(P32, "loaded64.txt")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                               ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                               ctypes.c_void_p]
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_size_t]
k32.VirtualQueryEx.restype = ctypes.c_size_t
k32.GetBinaryTypeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulong)]
k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k32.ResumeThread.argtypes = [wintypes.HANDLE]

psapi.GetMappedFileNameW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                     wintypes.LPWSTR, wintypes.DWORD]
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.LPWSTR, wintypes.DWORD]

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_CONSOLE = 0x00000010
MEM_IMAGE = 0x1000000
PROCESS_ALL = 0x1F0FFF
QUERY_INFO_VM_READ = 0x0410
LIST_32 = 0x01
LIST_ALL = 0x03
SCS = {0: "32BIT", 6: "64BIT"}
ADDR_LIMIT_32BIT = 0x100000000     # WOW64 的 32 位地址空间在低 4GB

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -- " + detail) if detail else ""))


class SI(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR), ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]


class PI(ctypes.Structure):
    _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]


class MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("PartitionId", wintypes.WORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD)]


def bitness_of_file(path):
    bt = ctypes.c_ulong(0)
    if k32.GetBinaryTypeW(ctypes.c_wchar_p(path), ctypes.byref(bt)):
        return SCS.get(bt.value, "?")
    return "?"


def rm(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def launch_suspended(exe, cwd):
    si = SI()
    si.cb = ctypes.sizeof(si)
    pi = PI()
    ok = k32.CreateProcessW(ctypes.c_wchar_p(exe), None, None, None, False,
                            CREATE_SUSPENDED, None, ctypes.c_wchar_p(cwd),
                            ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        raise RuntimeError("CreateProcessW(%s) failed err=%d"
                           % (exe, ctypes.get_last_error()))
    return pi


def launch_running(exe, cwd):
    return subprocess.Popen([exe], cwd=cwd, creationflags=CREATE_NEW_CONSOLE)


def enum_kernel32(pid, flag):
    """返回 (base|None, 模块数, err)"""
    h = k32.OpenProcess(QUERY_INFO_VM_READ, False, pid)
    if not h:
        return None, 0, ctypes.get_last_error()
    try:
        need = wintypes.DWORD(0)
        k32.SetLastError(0)
        r = psapi.EnumProcessModulesEx(h, None, 0, ctypes.byref(need), flag)
        err = ctypes.get_last_error()
        if not need.value:
            return None, 0, err
        buf = ctypes.create_string_buffer(need.value)
        if not psapi.EnumProcessModulesEx(h, buf, need.value, ctypes.byref(need), flag):
            return None, 0, ctypes.get_last_error()
        step = ctypes.sizeof(ctypes.c_void_p)
        n = need.value // step
        for i in range(n):
            base = ctypes.c_void_p.from_buffer(buf, i * step).value
            nm = ctypes.create_unicode_buffer(512)
            if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(base), nm, 512):
                if os.path.basename(nm.value).lower() == "kernel32.dll":
                    return base, n, 0
        return None, n, 0
    finally:
        k32.CloseHandle(h)


def scan_mapped(pid, max_addr=ADDR_LIMIT_32BIT):
    """遍历目标地址空间的映像区域。不依赖 loader (PEB->Ldr), 只看 section 映射。
    返回 [(base, 名字小写), ...] 按地址升序。"""
    h = k32.OpenProcess(QUERY_INFO_VM_READ, False, pid)
    if not h:
        return []
    out = []
    try:
        addr = 0
        guard = 0
        while addr < max_addr and guard < 500000:
            guard += 1
            mbi = MBI()
            if k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                  ctypes.sizeof(mbi)) == 0:
                break
            base = mbi.BaseAddress or 0
            size = mbi.RegionSize or 0
            if size == 0:
                break
            if mbi.Type == MEM_IMAGE:
                nm = ctypes.create_unicode_buffer(1024)
                if psapi.GetMappedFileNameW(h, ctypes.c_void_p(base), nm, 1024):
                    alloc = mbi.AllocationBase or base
                    out.append((alloc, os.path.basename(nm.value).lower()))
            addr = base + size
    finally:
        k32.CloseHandle(h)
    # 去重(同一模块多个 section), 按基址排序
    seen = set()
    uniq = []
    for b, n in sorted(out):
        if (b, n) not in seen:
            seen.add((b, n))
            uniq.append((b, n))
    return uniq


def is_alive(pid):
    h = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED
    if not h:
        return False
    try:
        code = wintypes.DWORD(0)
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == 259   # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def wait_file(path, timeout=10.0, poll=0.005):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(path):
            return True, time.time() - t0
        time.sleep(poll)
    return False, time.time() - t0


def run_inject32(pid, dll):
    r = subprocess.run([INJECT32, str(pid), dll], capture_output=True,
                       text=True, timeout=30)
    return (r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip())


def kill_pi(pi, resume_first=False):
    try:
        if resume_first:
            k32.ResumeThread(pi.hThread)
        k32.TerminateProcess(pi.hProcess, 0)
        k32.CloseHandle(pi.hThread)
        k32.CloseHandle(pi.hProcess)
    except Exception:
        pass


def main():
    print("=== 位数预检 ===")
    for p in (TARGET32, INJECT32, PROBE32, PROBE64):
        print("  %-14s : %s" % (os.path.basename(p), bitness_of_file(p)))

    # ---------------------------------------------------------------
    print("\n=== (A) 32 位 KnownDLL 基址在同一 boot 内是否跨进程一致 ===")
    p1 = launch_running(TARGET32, P32)
    p2 = launch_running(TARGET32, P32)
    time.sleep(1.5)   # 必须等 loader 建好模块链, 否则枚举为空(上一版探针的竞态)
    b1, n1, e1 = enum_kernel32(p1.pid, LIST_32)
    b2, n2, e2 = enum_kernel32(p2.pid, LIST_32)
    print("  target32#1 pid=%d kernel32=0x%X (%d 模块, err=%d)"
          % (p1.pid, b1 or 0, n1, e1))
    print("  target32#2 pid=%d kernel32=0x%X (%d 模块, err=%d)"
          % (p2.pid, b2 or 0, n2, e2))
    check("运行中 32 位进程模块表可枚举", b1 is not None and b2 is not None,
          "err=%d/%d" % (e1, e2))
    check("两个独立 32 位进程 kernel32 基址相同 (KnownDLL 同址前提)",
          b1 is not None and b1 == b2, "0x%X vs 0x%X" % (b1 or 0, b2 or 0))

    # inject32.exe 自己的 kernel32 基址(用不存在 pid 走完打印)
    rc, out, err = run_inject32(0xFFFFFFFE, PROBE32)
    self_k32 = 0
    for line in out.splitlines():
        if line.startswith("SELF_K32="):
            self_k32 = int(line.split("=")[1], 16)
    print("  inject32.exe 自身 kernel32=0x%X" % self_k32)
    check("inject32.exe 的 kernel32 基址 == 目标进程基址",
          self_k32 != 0 and self_k32 == b1, "0x%X vs 0x%X" % (self_k32, b1 or 0))

    for p in (p1, p2):
        p.terminate()

    # ---------------------------------------------------------------
    print("\n=== (B) 挂起的 32 位进程里映射了哪些映像 (改用内存映射扫描) ===")
    t = launch_suspended(TARGET32, P32)
    print("  挂起 pid=%d" % t.dwProcessId)
    t0 = time.time()
    maps = scan_mapped(t.dwProcessId)
    print("  扫描耗时 %.0f ms, 共 %d 个映像区域"
          % ((time.time() - t0) * 1000, len(maps)))
    for b, n in maps:
        print("    0x%08X  %s" % (b, n))
    names = set(n for _, n in maps)
    check("挂起态能扫到 ntdll.dll", "ntdll.dll" in names)
    check("挂起态能扫到目标 exe 自身", any("target32" in n for n in names))
    print("  注: 挂起态是否已映射 kernel32 -> %s"
          % ("是" if "kernel32.dll" in names else "否"))

    # ---------------------------------------------------------------
    print("\n=== (C) 【核心】32 位注入器 -> 挂起的 32 位进程 ===")
    rm(MARK32)
    rc, out, err = run_inject32(t.dwProcessId, PROBE32)
    print("  inject32.exe 退出码=%d" % rc)
    for line in out.splitlines():
        print("    | " + line)
    if err:
        print("    stderr: " + err)
    found, el = wait_file(MARK32, timeout=10)
    print("  目标进程存活: %s" % is_alive(t.dwProcessId))
    check("注入返回成功(远程 LoadLibraryW != 0)", rc == 0,
          "退出码=%d" % rc)
    check("【核心】probe32.dll 真的在挂起的 32 位目标里加载(loaded32.txt)",
          found, "%.0f ms" % (el * 1000))

    # ---------------------------------------------------------------
    print("\n=== (D) 对照: 32 位注入器 -> 运行中的 32 位进程 ===")
    kill_pi(t, resume_first=True)
    p3 = launch_running(TARGET32, P32)
    time.sleep(1.2)
    rm(MARK32)
    rc3, out3, err3 = run_inject32(p3.pid, PROBE32)
    print("  inject32.exe 退出码=%d" % rc3)
    for line in out3.splitlines():
        print("    | " + line)
    found3, el3 = wait_file(MARK32, timeout=10)
    check("对照: 向运行中的 32 位进程注入成功", rc3 == 0 and found3,
          "rc=%d found=%s" % (rc3, found3))
    p3.terminate()

    # ---------------------------------------------------------------
    print("\n=== (E) 64 位侧向挂起的 32 位进程注入 64 位 DLL (路线分歧点) ===")
    t2 = launch_suspended(TARGET32, P32)
    rm(MARK64)
    # 用本地 x64 kernel32!LoadLibraryW 地址
    pfn = ctypes.cast(k32.LoadLibraryW, ctypes.c_void_p).value
    h = k32.OpenProcess(PROCESS_ALL, False, t2.dwProcessId)
    ok_e = False
    detail = ""
    if not h:
        detail = "OpenProcess err=%d" % ctypes.get_last_error()
    else:
        try:
            data = ctypes.create_unicode_buffer(PROBE64)
            size = ctypes.sizeof(data)
            k32.VirtualAllocEx.restype = ctypes.c_void_p
            addr = k32.VirtualAllocEx(h, None, size, 0x3000, 0x04)
            w = ctypes.c_size_t(0)
            k32.WriteProcessMemory(h, ctypes.c_void_p(addr),
                                   ctypes.cast(data, ctypes.c_void_p), size,
                                   ctypes.byref(w))
            th = k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(pfn),
                                        ctypes.c_void_p(addr), 0, None)
            if th:
                wait = k32.WaitForSingleObject(th, 10000)
                code = wintypes.DWORD(0)
                k32.GetExitCodeThread(th, ctypes.byref(code))
                k32.CloseHandle(th)
                ok_e = (wait == 0 and code.value != 0)
                detail = "wait=%d exitcode=0x%X" % (wait, code.value)
            else:
                detail = "CreateRemoteThread err=%d" % ctypes.get_last_error()
        finally:
            k32.CloseHandle(h)
    found64, el64 = wait_file(MARK64, timeout=5)
    print("  目标进程存活: %s" % is_alive(t2.dwProcessId))
    check("64 位 DLL 能否注入挂起的 32 位进程", ok_e and found64,
          "%s, loaded64=%s" % (detail, found64))

    if found64:
        print("\n  -> (E) 成功后, 再看该进程里 32 位侧是否已初始化:")
        maps2 = scan_mapped(t2.dwProcessId)
        for b, n in maps2:
            print("    0x%08X  %s" % (b, n))
        names2 = set(n for _, n in maps2)
        check("64 位注入后挂起进程里出现 kernel32", "kernel32.dll" in names2)
    else:
        print("\n  -> (E) 失败: 挂起的 WOW64 进程里 64 位线程也起不来/加载失败")
    kill_pi(t2, resume_first=True)

    # ---------------------------------------------------------------
    print("\n=== 汇总 ===")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, det in results:
        print("  %-4s %s" % ("PASS" if ok else "FAIL", name))
    print("\n%d/%d 项通过" % (passed, len(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
