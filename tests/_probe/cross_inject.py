# -*- coding: utf-8 -*-
"""cross_inject.py -- 判定: x64 进程能否把 32 位 DLL 注入进 32 位进程?

这是决定"是否必须另做 32 位注入器"的关键实验。

方法: 从 x64 Python 出发, 对 32 位 target32.exe 做远程 LoadLibraryW。
      用 EnumProcessModulesEx(LIST_MODULES_32BIT) 取目标内 32 位 kernel32 基址,
      从磁盘上的 SysWOW64\\kernel32.dll 解析 LoadLibraryW 的 RVA,
      再 CreateRemoteThread 起远程线程。
判据: 目标模块表出现 probe32.dll, 或痕迹文件 loaded32.txt 生成。
"""
import ctypes
import os
import struct
import subprocess
import sys
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
P32 = os.path.join(HERE, "probe32")
DLL32 = os.path.join(P32, "probe32.dll")
TARGET32 = os.path.join(P32, "target32.exe")
MARK = os.path.join(P32, "loaded32.txt")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

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
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.GetExitCodeThread.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k32.GetBinaryTypeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_ulong)]

psapi.EnumProcessModulesEx.restype = wintypes.BOOL
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD]
psapi.GetModuleFileNameExW.restype = wintypes.DWORD
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.LPWSTR, wintypes.DWORD]

PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT_RESERVE = 0x3000
PAGE_READWRITE = 0x04
LIST_MODULES_32BIT = 0x01
LIST_MODULES_ALL = 0x03
SCS = {0: "32BIT", 6: "64BIT"}


def bitness(path):
    bt = ctypes.c_ulong(0)
    if k32.GetBinaryTypeW(ctypes.c_wchar_p(path), ctypes.byref(bt)):
        return SCS.get(bt.value, "?")
    return "?"


def enum_modules(pid, flag):
    """返回 [(base, basename), ...]"""
    h = k32.OpenProcess(0x0410, False, pid)  # QUERY_INFORMATION|VM_READ
    if not h:
        return []
    try:
        need = wintypes.DWORD(0)
        psapi.EnumProcessModulesEx(h, None, 0, ctypes.byref(need), flag)
        if not need.value:
            return []
        buf = ctypes.create_string_buffer(need.value)
        if not psapi.EnumProcessModulesEx(h, buf, need.value, ctypes.byref(need), flag):
            return []
        out = []
        n = need.value // ctypes.sizeof(ctypes.c_void_p)
        for i in range(n):
            base = ctypes.c_void_p.from_buffer(buf, i * ctypes.sizeof(ctypes.c_void_p)).value
            name = ctypes.create_unicode_buffer(512)
            if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(base), name, 512):
                out.append((base, os.path.basename(name.value).lower()))
        return out
    finally:
        k32.CloseHandle(h)


def export_rva(dll_path, func_name):
    """从磁盘 PE 文件解析导出函数 RVA(与位数无关)。"""
    d = open(dll_path, "rb").read()
    e = struct.unpack_from("<I", d, 0x3C)[0]
    nsec = struct.unpack_from("<H", d, e + 6)[0]
    opt_size = struct.unpack_from("<H", d, e + 20)[0]
    opt = e + 24
    magic = struct.unpack_from("<H", d, opt)[0]
    is64 = (magic == 0x20B)
    secs = []
    s = opt + opt_size
    for i in range(nsec):
        o = s + i * 40
        vsz, va, rsz, ra = struct.unpack_from("<IIII", d, o + 8)
        secs.append((va, vsz, ra, rsz))

    def r2o(rva):
        for va, vsz, ra, rsz in secs:
            if va <= rva < va + max(vsz, rsz):
                return ra + (rva - va)
        return None

    dd_off = opt + (0x70 if is64 else 0x60)
    exp_rva = struct.unpack_from("<I", d, dd_off + 0 * 8)[0]
    if not exp_rva:
        return None
    eo = r2o(exp_rva)
    nnames = struct.unpack_from("<I", d, eo + 24)[0]
    addr_names = struct.unpack_from("<I", d, eo + 32)[0]
    addr_funcs = struct.unpack_from("<I", d, eo + 28)[0]
    no = r2o(addr_names)
    fo = r2o(addr_funcs)
    for i in range(nnames):
        nr = struct.unpack_from("<I", d, no + i * 4)[0]
        o2 = r2o(nr)
        end = d.index(b"\0", o2)
        nm = d[o2:end].decode("latin1")
        if nm == func_name:
            idx = struct.unpack_from("<H", d, r2o(struct.unpack_from("<I", d, eo + 36)[0]) + i * 2)[0]
            return struct.unpack_from("<I", d, fo + idx * 4)[0]
    return None


def main():
    print("probe32.dll  位数:", bitness(DLL32))
    print("target32.exe 位数:", bitness(TARGET32))
    try:
        os.remove(MARK)
    except OSError:
        pass

    p = subprocess.Popen([TARGET32], cwd=P32,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    time.sleep(1.5)
    print("32 位目标 pid =", p.pid)

    # 1. 目标内 32 位模块表 -> 找 32 位 kernel32 基址
    mods32 = enum_modules(p.pid, LIST_MODULES_32BIT)
    print("目标内 32 位模块数 =", len(mods32))
    k32_base = None
    for base, name in mods32:
        if name == "kernel32.dll":
            k32_base = base
            break
    print("32 位 kernel32 基址 = 0x%X" % (k32_base or 0))

    if not k32_base:
        print("未能取到 32 位 kernel32 基址, 无法继续")
        p.terminate()
        return 1

    # 2. LoadLibraryW 的 RVA(从 SysWOW64 的 kernel32 解析)
    rva = export_rva(r"C:\Windows\SysWOW64\kernel32.dll", "LoadLibraryW")
    print("LoadLibraryW RVA(SysWOW64) = 0x%X" % (rva or 0))
    pfn32 = (k32_base or 0) + (rva or 0)
    print("远程 32 位 LoadLibraryW 地址 = 0x%X" % pfn32)

    # 3. 远程写 DLL 路径 + CreateRemoteThread
    h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, p.pid)
    if not h:
        print("OpenProcess 失败 err=%d" % ctypes.get_last_error())
        p.terminate()
        return 1
    data = ctypes.create_unicode_buffer(DLL32)
    size = ctypes.sizeof(data)
    addr = k32.VirtualAllocEx(h, None, size, MEM_COMMIT_RESERVE, PAGE_READWRITE)
    print("VirtualAllocEx -> 0x%X" % (addr or 0))
    w = ctypes.c_size_t(0)
    okw = k32.WriteProcessMemory(h, ctypes.c_void_p(addr),
                                 ctypes.cast(data, ctypes.c_void_p), size,
                                 ctypes.byref(w))
    print("WriteProcessMemory ok=%d" % okw)

    th = k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(pfn32),
                                ctypes.c_void_p(addr), 0, None)
    print("CreateRemoteThread ->", th, "err=%d" % ctypes.get_last_error())
    if th:
        k32.WaitForSingleObject(th, 8000)
        code = wintypes.DWORD(0)
        k32.GetExitCodeThread(th, ctypes.byref(code))
        print("远程线程退出码 = 0x%X" % code.value)
        k32.CloseHandle(th)

    time.sleep(1.0)

    # 4. 判据: 模块表 / 痕迹文件
    mods32b = enum_modules(p.pid, LIST_MODULES_32BIT)
    loaded = any("probe32" in n for _, n in mods32b)
    mark = os.path.exists(MARK)
    print("\n=== 结论 ===")
    print("目标模块表含 probe32.dll :", loaded)
    print("痕迹文件 loaded32.txt 生成:", mark)
    print("=> x64 进程%s把 32 位 DLL 注入 32 位进程" % ("" if (loaded or mark) else "【不能】"))

    try:
        p.terminate()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
