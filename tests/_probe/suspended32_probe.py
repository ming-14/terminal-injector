# -*- coding: utf-8 -*-
"""suspended32_probe.py -- 判定: 挂起的 32 位（WOW64）进程能否枚举出 32 位模块表

为什么关键:
  x64 的 injected.dll 要给【挂起的 32 位子进程】(pwsh -> py.exe) 注入 32 位
  relay32.dll。跨位数注入必须拿到目标内 LoadLibraryW 的地址 = 目标 32 位
  kernel32 基址 + RVA(SysWOW64\\kernel32.dll)。而基址此前只能靠枚举目标
  32 位模块表获得 —— 而挂起进程的模块表实测为空(64 位情况, err=299)。

  若 32 位挂起进程的模块表【可枚举】-> 直接沿用 cross_inject 的做法, 全部搞定。
  若【不可枚举】-> 需要另找 32 位 kernel32 基址的可靠来源。

顺带记录: 主线程上下文 (Rip) 指向哪里 —— 作为备选方案的依据。

对照: 同一进程正常运行时枚举结果。
"""
import ctypes
import os
import struct
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
P32 = os.path.join(HERE, "probe32")
TARGET32 = os.path.join(P32, "target32.exe")   # 32 位, 长命

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                               ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD,
                               ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                               ctypes.c_void_p]
k32.OpenProcess.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.GetThreadContext.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
k32.ResumeThread.argtypes = [wintypes.HANDLE]
k32.GetBinaryTypeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulong)]

psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.LPWSTR, wintypes.DWORD]

ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.ULONG,
                                            ctypes.POINTER(wintypes.ULONG)]

PROCESS_ALL = 0x1F0FFF
CREATE_SUSPENDED = 0x00000004
LIST_32 = 0x01
LIST_64 = 0x02
LIST_ALL = 0x03
ProcessBasicInformation = 0
ProcessWow64Information = 26

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


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("Reserved3", ctypes.c_void_p)]


class CONTEXT32(ctypes.Structure):
    """x86 CONTEXT, 只取到 Eip 之前需要的字段。"""
    _fields_ = [("ContextFlags", wintypes.DWORD),
                ("Dr0", wintypes.DWORD), ("Dr1", wintypes.DWORD),
                ("Dr2", wintypes.DWORD), ("Dr3", wintypes.DWORD),
                ("Dr6", wintypes.DWORD), ("Dr7", wintypes.DWORD),
                ("FloatSave", ctypes.c_byte * 112),
                ("SegGs", wintypes.DWORD), ("SegFs", wintypes.DWORD),
                ("SegEs", wintypes.DWORD), ("SegDs", wintypes.DWORD),
                ("Edi", wintypes.DWORD), ("Esi", wintypes.DWORD),
                ("Ebx", wintypes.DWORD), ("Edx", wintypes.DWORD),
                ("Ecx", wintypes.DWORD), ("Eax", wintypes.DWORD),
                ("Ebp", wintypes.DWORD), ("Eip", wintypes.DWORD),
                ("SegCs", wintypes.DWORD), ("EFlags", wintypes.DWORD),
                ("Esp", wintypes.DWORD), ("SegSs", wintypes.DWORD),
                ("ExtendedRegisters", ctypes.c_byte * 512)]


CONTEXT_CONTROL_X86 = 0x00010001
CONTEXT_INTEGER_X86 = 0x00010002
CONTEXT_FULL_X86 = 0x00010007


def enum_modules(pid, flag):
    h = k32.OpenProcess(0x0410, False, pid)
    if not h:
        return [], -1, "OpenProcess"
    try:
        need = wintypes.DWORD(0)
        r = psapi.EnumProcessModulesEx(h, None, 0, ctypes.byref(need), flag)
        err = ctypes.get_last_error()
        if not r or not need.value:
            return [], err, "query"
        buf = ctypes.create_string_buffer(need.value)
        if not psapi.EnumProcessModulesEx(h, buf, need.value, ctypes.byref(need), flag):
            return [], ctypes.get_last_error(), "enum"
        out = []
        step = ctypes.sizeof(ctypes.c_void_p)
        for i in range(need.value // step):
            base = ctypes.c_void_p.from_buffer(buf, i * step).value
            nm = ctypes.create_unicode_buffer(512)
            if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(base), nm, 512):
                out.append((base, os.path.basename(nm.value).lower()))
        return out, 0, "ok"
    finally:
        k32.CloseHandle(h)


def read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
        return None
    return buf.raw


def launch(suspended):
    si = SI()
    si.cb = ctypes.sizeof(si)
    pi = PI()
    flags = CREATE_SUSPENDED if suspended else 0
    ok = k32.CreateProcessW(ctypes.c_wchar_p(TARGET32), None, None, None, False,
                            flags, None, ctypes.c_wchar_p(P32),
                            ctypes.byref(si), ctypes.byref(pi))
    if not ok:
        print("CreateProcessW failed err=%d" % ctypes.get_last_error())
        sys.exit(1)
    return pi


def main():
    bt = ctypes.c_ulong(0)
    k32.GetBinaryTypeW(ctypes.c_wchar_p(TARGET32), ctypes.byref(bt))
    print("目标: %s 位数=%s" % (os.path.basename(TARGET32),
                                {0: "32BIT", 6: "64BIT"}.get(bt.value, bt.value)))

    print("\n=== 对照组: 正常运行中的 32 位进程 ===")
    pi_run = launch(suspended=False)
    time.sleep(1.2)
    mods, err, st = enum_modules(pi_run.dwProcessId, LIST_32)
    print("  LIST_MODULES_32BIT -> %d 个模块, err=%d (%s)" % (len(mods), err, st))
    k32_base_run = next((b for b, n in mods if n == "kernel32.dll"), None)
    print("  32 位 kernel32 基址 = 0x%X" % (k32_base_run or 0))
    check("运行中 32 位进程模块表可枚举", len(mods) > 0 and k32_base_run is not None,
          "%d 模块" % len(mods))

    print("\n=== 实验组: CREATE_SUSPENDED 的 32 位进程 ===")
    pi = launch(suspended=True)
    pid = pi.dwProcessId
    print("  pid = %d (已挂起)" % pid)

    for name, flag in (("LIST_MODULES_32BIT", LIST_32),
                       ("LIST_MODULES_ALL", LIST_ALL),
                       ("LIST_MODULES_64BIT", LIST_64)):
        mods, err, st = enum_modules(pid, flag)
        print("  %-20s -> %d 个模块, err=%d (%s)" % (name, len(mods), err, st))

    mods32, err32, _ = enum_modules(pid, LIST_32)
    if mods32:
        bases = [b for b, n in mods32 if n == "kernel32.dll"]
        print("  !! 挂起态也能枚举到 32 位 kernel32 基址 = 0x%X" % (bases[0] if bases else 0))
    check("挂起态【可以】枚举 32 位模块表", len(mods32) > 0,
          "err=%d" % err32)

    # 备选依据: 主线程上下文 + PEB
    h = k32.OpenProcess(PROCESS_ALL, False, pid)
    if h:
        ctx = CONTEXT32()
        ctx.ContextFlags = CONTEXT_FULL_X86
        if k32.GetThreadContext(pi.hThread, ctypes.byref(ctx)):
            print("\n  主线程上下文(x86): Eip=0x%08X Esp=0x%08X Ecx=0x%08X Edx=0x%08X"
                  % (ctx.Eip, ctx.Esp, ctx.Ecx, ctx.Edx))
        else:
            print("\n  GetThreadContext 失败 err=%d" % ctypes.get_last_error())

        pbi = PROCESS_BASIC_INFORMATION()
        ret = wintypes.ULONG(0)
        st = ntdll.NtQueryInformationProcess(h, ProcessBasicInformation,
                                             ctypes.byref(pbi),
                                             ctypes.sizeof(pbi), ctypes.byref(ret))
        print("  NtQueryInformationProcess(Basic) status=0x%X Peb=0x%X"
              % (st & 0xFFFFFFFF, pbi.PebBaseAddress or 0))
        if st == 0 and pbi.PebBaseAddress:
            ldr = read_mem(h, pbi.PebBaseAddress + 0x0C, 8)
            if ldr:
                p_ldr = struct.unpack("<Q", ldr)[0]
                print("  64 位视角 PEB->Ldr = 0x%X" % p_ldr)

        wow = ctypes.c_void_p(0)
        st2 = ntdll.NtQueryInformationProcess(h, ProcessWow64Information,
                                              ctypes.byref(wow), ctypes.sizeof(wow),
                                              ctypes.byref(ret))
        print("  ProcessWow64Information status=0x%X 32位PEB=0x%X"
              % (st2 & 0xFFFFFFFF, wow.value or 0))
        if st2 == 0 and wow.value:
            raw = read_mem(h, wow.value + 0x0C, 4)   # PEB32->Ldr (32 位指针)
            if raw:
                p_ldr32 = struct.unpack("<I", raw)[0]
                print("  PEB32->Ldr = 0x%08X" % p_ldr32)
                if p_ldr32:
                    hdr = read_mem(h, p_ldr32, 16)
                    if hdr:
                        length, initialized = struct.unpack_from("<IB", hdr, 0)[0], hdr[4]
                        head = struct.unpack_from("<II", hdr, 0x14)
                        print("  Ldr32.Length=%u Initialized=%d InMemoryOrder.Flink=0x%08X"
                              % (length, initialized, head[0]))
                        # 试着走链: 若 Flink == &InMemoryOrderModuleList 说明表为空
                        print("  列表为空(自指)? %s" % (head[0] == p_ldr32 + 0x14))
        k32.CloseHandle(h)

    print("\n=== 恢复并清理 ===")
    k32.ResumeThread(pi.hThread)
    time.sleep(0.3)
    for p in (pi_run, pi):
        try:
            k32.TerminateProcess(p.hProcess, 0)
        except Exception:
            pass
        k32.CloseHandle(p.hThread)
        k32.CloseHandle(p.hProcess)

    print("\n=== 汇总 ===")
    for name, ok in results:
        print("  %-4s %s" % ("PASS" if ok else "FAIL", name))
    print("\n=> 判定: 挂起态 32 位模块表 %s 枚举"
          % ("可以" if results[-1][1] else "【不可以】"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
