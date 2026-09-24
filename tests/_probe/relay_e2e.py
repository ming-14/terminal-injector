# -*- coding: utf-8 -*-
"""relay_e2e.py -- 32 位中继 DLL 方案的端到端验证

验证目标(全部为可判定事实):

  [前提] x64 进程能否把 32 位 relay_probe.dll 注入 32 位 target32b.exe
  [环节1] relay_probe.dll 的 MinHook 是否真的钩住 CreateProcessW/A
  [环节2] 中继是否把子进程强制 CREATE_SUSPENDED 并冻住
  [环节3] 在子进程冻结期间, x64 侧能否向它(64 位)注入 64 位 probe64.dll
  [环节4] 中继收到 ack 后恢复子进程, 子进程是否正常运行
  [铁证]   gc64.exe 在 main 第一条语句检查 loaded64.txt 是否已存在。
          若在 -> 说明 DLL 在本进程 main 之前就已落地, 即进程确实被冻住过。
          该判据不依赖时间戳精度。

Python 在这里扮演真实实现中的 mediator: 中继=32位DLL, 本脚本=x64 进程侧。

用法: python relay_e2e.py
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
RELAY_DLL = os.path.join(P32, "relay_probe.dll")
TARGET = os.path.join(P32, "target32b.exe")
PROBE64 = os.path.join(P32, "probe64.dll")

NOTIFY = os.path.join(P32, "relay_notify.txt")
ACK = os.path.join(P32, "relay_ack.txt")
MARK32 = os.path.join(P32, "loaded32.txt")
MARK64 = os.path.join(P32, "loaded64.txt")
VERDICT = os.path.join(P32, "gc_verdict.txt")
GCDONE = os.path.join(P32, "gc_done.txt")
LOG = os.path.join(P32, "relay_log.txt")

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
k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                           wintypes.LPWSTR,
                                           ctypes.POINTER(wintypes.DWORD)]
k32.IsWow64Process.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]

psapi.EnumProcessModulesEx.restype = wintypes.BOOL
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD]
psapi.GetModuleFileNameExW.restype = wintypes.DWORD
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                       wintypes.LPWSTR, wintypes.DWORD]

PROCESS_ALL_ACCESS = 0x1F0FFF
PROCESS_QUERY_LIMITED = 0x1000
MEM_COMMIT_RESERVE = 0x3000
PAGE_READWRITE = 0x04
LIST_MODULES_32BIT = 0x01
LIST_MODULES_64BIT = 0x02
SCS = {0: "32BIT", 6: "64BIT"}

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           ("  -- " + detail) if detail else ""))


def bitness_of_file(path):
    bt = ctypes.c_ulong(0)
    if k32.GetBinaryTypeW(ctypes.c_wchar_p(path), ctypes.byref(bt)):
        return SCS.get(bt.value, "?")
    return "?"


def proc_image(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = wintypes.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(h)


def proc_bitness(pid):
    """OS 权威判定: 64 位进程内 IsWow64Process; 32 位 Python 无法用此路径,
    但本脚本跑在 64 位 Python 上, 所以可靠。"""
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return "?"
    try:
        w = wintypes.BOOL(False)
        if not k32.IsWow64Process(h, ctypes.byref(w)):
            return "?"
        return "32" if w.value else "64"
    finally:
        k32.CloseHandle(h)


def enum_modules(pid, flag):
    """返回 [(base, basename_lower), ...]"""
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
        step = ctypes.sizeof(ctypes.c_void_p)
        for i in range(need.value // step):
            base = ctypes.c_void_p.from_buffer(buf, i * step).value
            name = ctypes.create_unicode_buffer(512)
            if psapi.GetModuleFileNameExW(h, ctypes.c_void_p(base), name, 512):
                out.append((base, os.path.basename(name.value).lower()))
        return out
    finally:
        k32.CloseHandle(h)


def export_rva(dll_path, func_name):
    """从磁盘 PE 解析导出函数 RVA(与目标位数无关)。"""
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
    exp_rva = struct.unpack_from("<I", d, dd_off)[0]
    if not exp_rva:
        return None
    eo = r2o(exp_rva)
    nnames = struct.unpack_from("<I", d, eo + 24)[0]
    addr_names = struct.unpack_from("<I", d, eo + 32)[0]
    addr_funcs = struct.unpack_from("<I", d, eo + 28)[0]
    addr_ords = struct.unpack_from("<I", d, eo + 36)[0]
    no, fo, oo = r2o(addr_names), r2o(addr_funcs), r2o(addr_ords)
    for i in range(nnames):
        nr = struct.unpack_from("<I", d, no + i * 4)[0]
        o2 = r2o(nr)
        end = d.index(b"\0", o2)
        if d[o2:end].decode("latin1") == func_name:
            idx = struct.unpack_from("<H", d, oo + i * 2)[0]
            return struct.unpack_from("<I", d, fo + idx * 4)[0]
    return None


def local_loadlibraryw():
    """本进程 kernel32!LoadLibraryW 的实际地址(GetProcAddress 已解析转发到 kernelbase)。
    kernel32/kernelbase 是 KnownDLLs, 每个 boot 内所有同位数进程共享同一基址,
    因此该地址在 [同位数的] 目标进程内同样有效 —— 与 Injector.cpp 的做法一致。"""
    return ctypes.cast(k32.LoadLibraryW, ctypes.c_void_p).value


def inject_dll(pid, dll_path, bitness, timeout_ms=10000):
    """远程 LoadLibraryW 注入。

    bitness == "64": 调用方也是 64 位, 直接用本地 kernel32 地址。
                     这是唯一能在【挂起进程】上用的办法 —— 挂起进程的 loader
                     尚未运行, PEB 模块链表为空, EnumProcessModulesEx 取不到基址。
    bitness == "32": 本地地址是 64 位的, 不能用于 32 位目标, 必须从目标自身的
                     32 位模块表取 kernel32 基址。目标此时是运行中进程, 可枚举。
    """
    if bitness == "64":
        pfn = local_loadlibraryw()
        how = "local kernel32 (KnownDLL, 全进程同址)"
    else:
        flag = LIST_MODULES_32BIT
        mods = enum_modules(pid, flag)
        base = None
        for b, n in mods:
            if n == "kernel32.dll":
                base = b
                break
        if not base:
            return False, "target 32-bit kernel32 base not found (%d modules)" % len(mods)
        rva = export_rva(r"C:\Windows\SysWOW64\kernel32.dll", "LoadLibraryW")
        if not rva:
            return False, "LoadLibraryW rva not found"
        pfn = base + rva
        how = "target 32-bit module table (base=0x%X + rva=0x%X)" % (base, rva)

    h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not h:
        return False, "OpenProcess err=%d" % ctypes.get_last_error()
    try:
        data = ctypes.create_unicode_buffer(dll_path)
        size = ctypes.sizeof(data)
        addr = k32.VirtualAllocEx(h, None, size, MEM_COMMIT_RESERVE, PAGE_READWRITE)
        if not addr:
            return False, "VirtualAllocEx err=%d" % ctypes.get_last_error()
        w = ctypes.c_size_t(0)
        if not k32.WriteProcessMemory(h, ctypes.c_void_p(addr),
                                      ctypes.cast(data, ctypes.c_void_p), size,
                                      ctypes.byref(w)):
            return False, "WriteProcessMemory err=%d" % ctypes.get_last_error()
        th = k32.CreateRemoteThread(h, None, 0, ctypes.c_void_p(pfn),
                                    ctypes.c_void_p(addr), 0, None)
        if not th:
            return False, "CreateRemoteThread err=%d" % ctypes.get_last_error()
        wait = k32.WaitForSingleObject(th, timeout_ms)
        code = wintypes.DWORD(0)
        k32.GetExitCodeThread(th, ctypes.byref(code))
        k32.CloseHandle(th)
        if wait != 0:  # WAIT_OBJECT_0
            return False, "remote thread not finished (wait=%d)" % wait
        # 注意: 退出码只有 32 位, 64 位 HMODULE 高 32 位被截断, 仅用于判"是否非 0"
        return code.value != 0, "LoadLibraryW ret=0x%X via %s" % (code.value, how)
    finally:
        k32.CloseHandle(h)


def rm(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def wait_file(path, timeout=20.0, poll=0.01):
    """返回 (found, elapsed)"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(path):
            return True, time.time() - t0
        time.sleep(poll)
    return False, time.time() - t0


def main():
    print("=== 位数预检 ===")
    print("relay_probe.dll :", bitness_of_file(RELAY_DLL))
    print("target32b.exe   :", bitness_of_file(TARGET))
    print("probe64.dll     :", bitness_of_file(PROBE64))
    print("gc64.exe        :", bitness_of_file(PROBE64.replace("probe64.dll", "gc64.exe")))

    rm(NOTIFY, ACK, MARK32, MARK64, VERDICT, GCDONE, LOG)

    print("\n=== 启动 32 位启动器 ===")
    p = subprocess.Popen([TARGET], cwd=P32, creationflags=subprocess.CREATE_NEW_CONSOLE)
    print("target32b pid = %d, 位数 = %s" % (p.pid, proc_bitness(p.pid)))
    time.sleep(1.2)

    print("\n=== 环节0: x64 向 32 位进程注入 32 位中继 ===")
    ok, info = inject_dll(p.pid, RELAY_DLL, "32")
    check("x64->32bit 远程 LoadLibraryW", ok, info)
    found, el = wait_file(LOG, timeout=5)
    check("中继已执行 DllMain(relay_log.txt 生成)", found, "%.0f ms" % (el * 1000))
    if found:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            print("  relay_log:\n    " + f.read().replace("\n", "\n    ").strip())

    print("\n=== 环节1/2: 32 位启动器拉起 64 位孙进程 (target 睡 4s 后) ===")
    found, el = wait_file(NOTIFY, timeout=15)
    check("中继捕获 CreateProcessW 并写出 notify", found, "%.0f ms" % (el * 1000))
    if not found:
        print("  未收到 notify, 无法继续")
        p.terminate()
        return 1

    child_pid = int(open(NOTIFY).read().strip())
    child_img = proc_image(child_pid)
    child_bits = proc_bitness(child_pid)
    print("  孙进程 pid = %d" % child_pid)
    print("  孙进程镜像 = %s" % child_img)
    print("  孙进程位数 = %s  (由 IsWow64Process 判定)" % child_bits)
    check("孙进程确为 64 位", child_bits == "64", child_bits)

    print("\n=== 环节3: 在孙进程被冻结期间注入 64 位 DLL ===")
    # 先记录一个坑: 挂起进程的 PEB 模块链表尚未由 loader 建立, 枚举必然为空。
    # 因此注入不能走"枚举目标模块表取 kernel32 基址"这条路, 必须用本地地址。
    hq = k32.OpenProcess(0x0410, False, child_pid)
    need = wintypes.DWORD(0)
    r = psapi.EnumProcessModulesEx(hq, None, 0, ctypes.byref(need), LIST_MODULES_64BIT) if hq else 0
    err = ctypes.get_last_error()
    if hq:
        k32.CloseHandle(hq)
    print("  [已知坑] 挂起进程 EnumProcessModulesEx -> ret=%d, needed=%d, err=%d"
          % (r, need.value, err))
    check("挂起进程模块表为空 => 不能用模块表取基址(故走本地地址)",
          need.value == 0, "needed=%d" % need.value)

    t0 = time.time()
    ok, info = inject_dll(child_pid, PROBE64, "64")
    dt = (time.time() - t0) * 1000
    check("向【挂起中】的 64 位孙进程注入 probe64.dll", ok,
          "%s, 耗时 %.0f ms" % (info, dt))

    mods = [n for _, n in enum_modules(child_pid, LIST_MODULES_64BIT)]
    check("孙进程模块表已含 probe64.dll", any("probe64" in n for n in mods))

    # 写 ack, 中继才会 ResumeThread
    open(ACK, "w").write("ok\n")
    print("  已写 ack -> 中继恢复孙进程")

    print("\n=== 环节4: 孙进程恢复后应正常跑完 ===")
    found, el = wait_file(VERDICT, timeout=10)
    check("孙进程 main 已执行(gc_verdict.txt 生成)", found, "%.0f ms" % (el * 1000))
    found2, el2 = wait_file(GCDONE, timeout=10)
    check("孙进程正常结束(gc_done.txt 生成)", found2, "%.0f ms" % (el2 * 1000))

    verdict = ""
    if os.path.exists(VERDICT):
        verdict = open(VERDICT).read().strip()
        print("  gc_verdict: %s" % verdict)
    check("【铁证】probe64.dll 在孙进程 main 之前已加载(证明进程被冻住)",
          "probe64_loaded_before_main=1" in verdict, verdict)

    print("\n=== 完整 relay_log ===")
    if os.path.exists(LOG):
        print(open(LOG, encoding="utf-8", errors="replace").read().strip())

    try:
        p.terminate()
    except Exception:
        pass

    print("\n=== 汇总 ===")
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, det in results:
        print("  %-4s %s" % ("PASS" if ok else "FAIL", name))
    print("\n%d/%d 项通过" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
