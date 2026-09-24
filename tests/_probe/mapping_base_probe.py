# -*- coding: utf-8 -*-
"""mapping_base_probe.py -- 判定: 能否用内存映射区域(VirtualQueryEx)取到
【挂起】进程内某个 DLL 的基址

背景:
  跨位数注入必须知道目标内 LoadLibraryW 的地址 = 目标 kernel32 基址 + RVA。
  但挂起进程的 loader 未运行(PEB->Ldr=NULL), EnumProcessModulesEx 返回 err=299,
  枚举不到模块表。
  模块虽然枚举不到, 但 section 在进程创建时就已映射 -> VirtualQueryEx 应能看见,
  再用 GetMappedFileNameW 认名字即可定位基址。这条路径不依赖任何假设
  (不需要 KnownDLL 跨进程同址, 不需要另起 32 位助手, 不需要 shellcode)。

判据:
  1. 挂起态下能扫到 kernel32.dll 的映射区域
  2. 该基址 == 同一程序正常运行时的 kernel32 基址(交叉验证)
  3. 挂起态下能**同时**扫到 64 位目标(对照, 说明方法不是 32 位专有)
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
P32 = os.path.join(HERE, "probe32")
TARGET32 = os.path.join(P32, "target32.exe")
GC64 = os.path.join(P32, "gc64.exe")

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
psapi.GetMappedFileNameW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                     wintypes.LPWSTR, wintypes.DWORD]
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.LPWSTR, wintypes.DWORD]

CREATE_SUSPENDED = 0x00000004
MEM_IMAGE = 0x1000000
PROCESS_ALL = 0x1F0FFF
MAX_USER_ADDR_32 = 0x7FFFFFFF   # 32 位目标只需扫低 2GB
MAX_USER_ADDR_64 = 0x7FFFFFFF0000

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
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


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p),
                ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD),
                ("PartitionId", wintypes.WORD),
                ("RegionSize", ctypes.c_size_t),
                ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD),
                ("Type", wintypes.DWORD)]


def launch(exe, cwd, suspended):
    si = SI()
    si.cb = ctypes.sizeof(si)
    pi = PI()
    ok = k32.CreateProcessW(ctypes.c_wchar_p(exe), None, None, None, False,
                            CREATE_SUSPENDED if suspended else 0,
                            None, ctypes.c_wchar_p(cwd),
                            ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        print("CreateProcessW(%s) failed err=%d" % (exe, ctypes.get_last_error()))
        return None
    return pi


def scan_mapped(h, max_addr):
    """扫内存映射区域, 返回 {basename_lower: [base, ...]}"""
    found = {}
    addr = 0
    step_guard = 0
    while addr < max_addr and step_guard < 200000:
        step_guard += 1
        mbi = MEMORY_BASIC_INFORMATION()
        n = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                               ctypes.sizeof(mbi))
        if n == 0:
            break
        base = mbi.BaseAddress or 0
        size = mbi.RegionSize or 0
        if size == 0:
            break
        if mbi.Type == MEM_IMAGE:
            nm = ctypes.create_unicode_buffer(1024)
            if psapi.GetMappedFileNameW(h, ctypes.c_void_p(base), nm, 1024):
                bn = os.path.basename(nm.value).lower()
                found.setdefault(bn, [])
                alloc = mbi.AllocationBase or base
                if alloc not in found[bn]:
                    found[bn].append(alloc)
        addr = base + size
    return found


def enum_kernel32(h, flag):
    need = wintypes.DWORD(0)
    r = psapi.EnumProcessModulesEx(h, None, 0, ctypes.byref(need), flag)
    if not r or not need.value:
        return None, ctypes.get_last_error()
    buf = ctypes.create_string_buffer(need.value)
    if not psapi.EnumProcessModulesEx(h, buf, need.value, ctypes.byref(need), flag):
        return None, ctypes.get_last_error()
    step = ctypes.sizeof(ctypes.c_void_p)
    for i in range(need.value // step):
        base = ctypes.c_void_p.from_buffer(buf, i * step).value
        nm = ctypes.create_unicode_buffer(512)
        if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(base), nm, 512):
            if os.path.basename(nm.value).lower() == "kernel32.dll":
                return base, 0
    return None, 0


def main():
    # --- 对照组: 运行中的 32 位进程 (模块表可枚举) ---
    print("=== 对照组: 运行中的 32 位进程 ===")
    pi_run = launch(TARGET32, P32, suspended=False)
    if pi_run is None:
        return 1
    h = k32.OpenProcess(PROCESS_ALL, False, pi_run.dwProcessId)
    base_enum, err = enum_kernel32(h, 0x01)
    print("  EnumProcessModulesEx(32BIT) kernel32 基址 = 0x%X (err=%d)"
          % (base_enum or 0, err))
    maps_run = scan_mapped(h, MAX_USER_ADDR_32)
    k32_maps_run = maps_run.get("kernel32.dll", [])
    print("  VirtualQueryEx 扫到 kernel32 区域基址 = %s"
          % ", ".join("0x%X" % b for b in k32_maps_run))
    check("运行态两种方法结果一致",
          base_enum is not None and base_enum in k32_maps_run,
          "enum=0x%X maps=%s" % (base_enum or 0,
                                 ",".join("0x%X" % b for b in k32_maps_run)))
    k32.CloseHandle(h)

    # --- 实验组: 挂起的 32 位进程 ---
    print("\n=== 实验组: CREATE_SUSPENDED 的 32 位进程 ===")
    pi = launch(TARGET32, P32, suspended=True)
    if pi is None:
        return 1
    h = k32.OpenProcess(PROCESS_ALL, False, pi.dwProcessId)
    base_enum_s, err_s = enum_kernel32(h, 0x01)
    print("  EnumProcessModulesEx(32BIT) -> %s (err=%d)"
          % ("0x%X" % base_enum_s if base_enum_s else "取不到", err_s))
    maps = scan_mapped(h, MAX_USER_ADDR_32)
    k32_maps = maps.get("kernel32.dll", [])
    print("  VirtualQueryEx 扫到 kernel32 区域基址 = %s"
          % ", ".join("0x%X" % b for b in k32_maps) or "（无）")
    ntdll_maps = maps.get("ntdll.dll", [])
    print("  同时扫到 ntdll 区域基址 = %s"
          % ", ".join("0x%X" % b for b in ntdll_maps))
    print("  共扫到 %d 个映像模块" % len(maps))
    check("挂起态用内存映射能取到 32 位 kernel32 基址", len(k32_maps) > 0)
    check("挂起态取到的基址 == 运行态基址(交叉验证)",
          base_enum is not None and base_enum in k32_maps,
          "hung=0x%X" % (k32_maps[0] if k32_maps else 0))
    check("挂起态同时扫到 ntdll(说明扫描确实覆盖了已映射的系统 DLL)",
          len(ntdll_maps) > 0)
    k32.CloseHandle(h)

    # --- 对照: 挂起的 64 位进程 ---
    print("\n=== 对照: CREATE_SUSPENDED 的 64 位进程 ===")
    pi64 = launch(GC64, P32, suspended=True)
    if pi64 is not None:
        h = k32.OpenProcess(PROCESS_ALL, False, pi64.dwProcessId)
        maps64 = scan_mapped(h, MAX_USER_ADDR_64)
        k64 = maps64.get("kernel32.dll", [])
        print("  64 位挂起进程扫到 kernel32 区域基址 = %s"
              % ", ".join("0x%X" % b for b in k64))
        check("方法对 64 位挂起进程同样有效", len(k64) > 0)
        k32.CloseHandle(h)
        k32.ResumeThread(pi64.hThread)
        k32.TerminateProcess(pi64.hProcess, 0)
        k32.CloseHandle(pi64.hThread)
        k32.CloseHandle(pi64.hProcess)

    k32.ResumeThread(pi_run.hThread)
    k32.ResumeThread(pi.hThread)
    time.sleep(0.2)
    for p in (pi_run, pi):
        k32.TerminateProcess(p.hProcess, 0)
        k32.CloseHandle(p.hThread)
        k32.CloseHandle(p.hProcess)

    print("\n=== 汇总 ===")
    for name, ok in results:
        print("  %-4s %s" % ("PASS" if ok else "FAIL", name))
    passed = sum(1 for _, ok in results if ok)
    print("\n%d/%d 项通过" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
