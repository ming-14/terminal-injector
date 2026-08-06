"""读 PEB Ldr 链表查 injected.dll 的 LoadCount（实际引用计数）。

背景（Phase 11 诊断）：
  FreeLibraryAndExitThread 已被调用（日志确认），但 DLL_PROCESS_DETACH
  未触发（DllMain.cpp 的 "=== injected.dll unloaded ===" 日志未出现）。
  说明 FreeLibrary 减引用计数后未归零，DLL 未真正卸载。

  ObsoleteLoadCount 字段（_LDR_DATA_TABLE_ENTRY+0x6c）在 Windows 10+
  不再可靠，实际 LoadCount 存于 _LDR_DDAG_NODE.LoadCount（偏移 0x18），
  通过 _LDR_DATA_TABLE_ENTRY+0x98 的 DdagNode 指针找到。

  注意：+0x20 是 LowestLink，不是 LoadCount（早期注释错误，已于
  2026-07-25 通过 cdb dt ntdll!_LDR_DDAG_NODE 校正）。

流程：
  1. 启动 cmd + mediator + WT，注入 DLL
  2. 关闭 WT 触发卸载
  3. 等待 DLL 日志出现 "FreeLibraryAndExitThread"（DoUnload 跑到末尾）
  4. 不 cleanup cmd，用 NtQueryInformationProcess 拿 PEB
  5. ReadProcessMemory 遍历 PEB.Ldr.InLoadOrderModuleList
  6. 找 BaseDllName=="injected.dll"，读 DllBase + DdagNode->LoadCount

x64 _PEB 偏移：
  +0x018 Ldr (PEB_LDR_DATA*)

x64 _PEB_LDR_DATA 偏移：
  +0x010 InLoadOrderModuleList (LIST_ENTRY: Flink, Blink)
  +0x020 InMemoryOrderModuleList
  +0x030 InInitializationOrderModuleList

x64 _LDR_DATA_TABLE_ENTRY 偏移（Windows 10/11）：
  +0x000 InLoadOrderLinks (LIST_ENTRY, 16B)
  +0x010 InMemoryOrderLinks (LIST_ENTRY, 16B)
  +0x020 InInitializationOrderLinks (LIST_ENTRY, 16B)
  +0x030 DllBase (PVOID, 8B)
  +0x038 EntryPoint (PVOID, 8B)
  +0x040 SizeOfImage (ULONG, 4B)
  +0x048 FullDllName (UNICODE_STRING, 16B: Len/MaxLen/Buffer)
  +0x058 BaseDllName (UNICODE_STRING, 16B)
  +0x068 Flags (ULONG, 4B)
  +0x06c ObsoleteLoadCount (USHORT, 2B) ← 旧字段，Win10+ 通常为 0xffff
  +0x06e TlsIndex (USHORT, 2B)
  +0x098 DdagNode (PVOID, 8B) → _LDR_DDAG_NODE*

x64 _LDR_DDAG_NODE 偏移（Windows 10/11，2026-07-25 通过 cdb dt 校正）：
  +0x000 Modules (LIST_ENTRY)
  +0x010 ServiceTagList (PVOID)
  +0x018 LoadCount (ULONG, 4B) ← 实际引用计数
  +0x01c LoadWhileUnloadingCount (ULONG, 4B)
  +0x020 LowestLink (ULONG, 4B) ← 最低链接数（不是 LoadCount）
  +0x028 Dependencies (_LDRP_CSLIST)
  +0x030 IncomingDependencies (_LDRP_CSLIST)
  +0x038 State (_LDR_DDAG_STATE)  9=LdrModulesReadyToUnload
  +0x040 CondenseLink (_SINGLE_LIST_ENTRY)
  +0x048 PreorderNumber (ULONG)

用法：python tests\helpers\diag_peb_loadcount.py
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

# ============================================================
# Win32 / Nt API 绑定
# ============================================================
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010

ProcessBasicInformation = 0

# PROCESS_BASIC_INFORMATION 返回结构（x64）
# ExitStatus 是 NTSTATUS（LONG），用 wintypes.LONG 代替
# AffinityMask 是 ULONG_PTR，用 ctypes.c_size_t（Python 3.10 wintypes 无 ULONG_PTR）
class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ExitStatus", wintypes.LONG),
        ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_size_t),
        ("BasePriority", wintypes.LONG),
        ("UniqueProcessId", ctypes.c_void_p),
        ("Reserved", ctypes.c_void_p),
    ]


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

_ntdll = ctypes.windll.ntdll
_ntdll.NtQueryInformationProcess.argtypes = [
    wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
    wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)
]
# NTSTATUS 是 LONG，用 wintypes.LONG 代替
_ntdll.NtQueryInformationProcess.restype = wintypes.LONG


# ============================================================
# 读取辅助
# ============================================================
def read_mem(hProc, addr, size):
    """ReadProcessMemory 包装，返回 bytes 或 None。"""
    if not addr:
        return None
    buf = (ctypes.c_ubyte * size)()
    bytesRead = ctypes.c_size_t(0)
    ok = _kernel32.ReadProcessMemory(hProc, ctypes.c_void_p(addr), buf, size,
                                      ctypes.byref(bytesRead))
    if not ok or bytesRead.value != size:
        return None
    return bytes(buf)


def read_ptr(hProc, addr):
    """读 8 字节指针。"""
    data = read_mem(hProc, addr, 8)
    if data is None:
        return None
    return int.from_bytes(data, "little")


def read_ushort(hProc, addr):
    """读 2 字节无符号短整。"""
    data = read_mem(hProc, addr, 2)
    if data is None:
        return None
    return int.from_bytes(data, "little")


def read_ulong(hProc, addr):
    """读 4 字节无符号整。"""
    data = read_mem(hProc, addr, 4)
    if data is None:
        return None
    return int.from_bytes(data, "little")


def read_unicode_string(hProc, addr):
    """读 UNICODE_STRING 结构（16B: Length/MaxLength/Buffer）并读字符串。"""
    data = read_mem(hProc, addr, 16)
    if data is None:
        return None
    length = int.from_bytes(data[0:2], "little")
    # maxlength = data[2:4]
    buf_ptr = int.from_bytes(data[8:16], "little")
    if length == 0 or not buf_ptr:
        return ""
    str_data = read_mem(hProc, buf_ptr, length)
    if str_data is None:
        return None
    try:
        return str_data.decode("utf-16-le")
    except UnicodeDecodeError:
        return None


# ============================================================
# 遍历 PEB Ldr 链表
# ============================================================
# _LDR_DATA_TABLE_ENTRY 偏移
OFF_DllBase = 0x30
OFF_SizeOfImage = 0x40
OFF_FullDllName = 0x48
OFF_BaseDllName = 0x58
OFF_Flags = 0x68
OFF_ObsoleteLoadCount = 0x6c
OFF_DdagNode = 0x98

# _LDR_DDAG_NODE 偏移（2026-07-25 校正：+0x18 是 LoadCount，+0x20 是 LowestLink）
OFF_DDAG_LoadCount = 0x18
OFF_DDAG_LowestLink = 0x20
OFF_DDAG_State = 0x38

# _PEB 偏移
OFF_PEB_Ldr = 0x18

# _PEB_LDR_DATA 偏移
OFF_LDR_InLoadOrderModuleList = 0x10


def find_injected_loadcount(pid, dump_bytes=False):
    """遍历 PEB.Ldr 找 injected.dll，返回 dict 或 None。

    dump_bytes=True 时额外 dump _LDR_DATA_TABLE_ENTRY 前 0xA0 字节和
    _LDR_DDAG_NODE 前 0x40 字节，用于定位真正的 LoadCount 偏移。
    """
    hProc = _kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
    if not hProc:
        print("  OpenProcess 失败 err={}".format(ctypes.get_last_error()))
        return None

    try:
        # 1. 拿 PEB 地址
        pbi = PROCESS_BASIC_INFORMATION()
        retLen = wintypes.ULONG(0)
        status = _ntdll.NtQueryInformationProcess(
            hProc, ProcessBasicInformation, ctypes.byref(pbi),
            ctypes.sizeof(pbi), ctypes.byref(retLen))
        if status != 0:
            print("  NtQueryInformationProcess 失败 status={:#x}".format(status))
            return None
        peb = pbi.PebBaseAddress
        if not peb:
            print("  PEB 为空")
            return None
        print("  PEB = {:#x}".format(peb))

        # 2. 读 PEB.Ldr
        ldr = read_ptr(hProc, peb + OFF_PEB_Ldr)
        if not ldr:
            print("  PEB.Ldr 为空")
            return None
        print("  PEB_LDR_DATA = {:#x}".format(ldr))

        # 3. InLoadOrderModuleList.Flink 是第一个 entry 的地址（即 entry+0x00）
        #    链表头地址 = ldr + OFF_LDR_InLoadOrderModuleList
        #    循环条件：cur 回到链表头（cur == head）时结束
        head_addr = ldr + OFF_LDR_InLoadOrderModuleList
        cur = read_ptr(hProc, head_addr)
        if not cur:
            print("  InLoadOrderModuleList.Flink 为空")
            return None

        # 4. 遍历链表，直到回到 head_addr
        entries = []
        max_iter = 512  # 防御无限循环
        while cur and cur != head_addr and max_iter > 0:
            # cur 指向 entry.InLoadOrderLinks，即 entry 基址
            base_dll = read_unicode_string(hProc, cur + OFF_BaseDllName)
            full_dll = read_unicode_string(hProc, cur + OFF_FullDllName)
            dll_base = read_ptr(hProc, cur + OFF_DllBase)
            size_img = read_ulong(hProc, cur + OFF_SizeOfImage)
            flags = read_ulong(hProc, cur + OFF_Flags)
            obs_loadcount = read_ushort(hProc, cur + OFF_ObsoleteLoadCount)
            ddag = read_ptr(hProc, cur + OFF_DdagNode)
            real_loadcount = None
            ddag_dump = None
            entry_dump = None
            if ddag:
                real_loadcount = read_ulong(hProc, ddag + OFF_DDAG_LoadCount)
                if dump_bytes:
                    ddag_dump = read_mem(hProc, ddag, 0x40)
            if dump_bytes:
                entry_dump = read_mem(hProc, cur, 0xA0)

            entries.append({
                "base_dll": base_dll or "",
                "full_dll": full_dll or "",
                "dll_base": dll_base or 0,
                "size": size_img or 0,
                "flags": flags or 0,
                "obs_loadcount": obs_loadcount or 0,
                "ddag": ddag or 0,
                "loadcount": real_loadcount,
                "entry_dump": entry_dump,
                "ddag_dump": ddag_dump,
                "entry_addr": cur,
            })

            # 下一个
            cur = read_ptr(hProc, cur)  # InLoadOrderLinks.Flink
            max_iter -= 1

        # 5. 找 injected.dll
        print("  共遍历 {} 个模块".format(len(entries)))
        for e in entries:
            if "injected" in e["base_dll"].lower():
                return e
        return None
    finally:
        _kernel32.CloseHandle(hProc)


def hex_dump(data, base_addr=0, prefix="  "):
    """格式化 bytes 为 hex dump，每行 16 字节。"""
    if not data:
        return
    for i in range(0, len(data), 16):
        chunk = data[i:i+16]
        hex_part = " ".join("{:02x}".format(b) for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("{prefix}{addr:08x}  {hex:<48}  {ascii}".format(
            prefix=prefix, addr=base_addr + i, hex=hex_part, ascii=ascii),
            flush=True)


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
def main():

    # 清旧 DLL 日志
    for f in glob.glob(paths.injected_log_glob()):
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

    # 卸载前查一次 LoadCount（基线，启用 dump_bytes）
    print("\n[pre-unload] 卸载前 LoadCount 检查（含字节 dump）...", flush=True)
    pre = find_injected_loadcount(target_pid, dump_bytes=True)
    if pre:
        print("  卸载前: base_dll={base_dll} dll_base={dll_base:#x} "
              "loadcount={loadcount} obs={obs_loadcount} flags={flags:#x}".format(
                  **pre))
        print("  entry_addr={:#x} ddag={:#x}".format(
            pre["entry_addr"], pre["ddag"]), flush=True)
        print("  _LDR_DATA_TABLE_ENTRY dump (前 0xA0 字节):", flush=True)
        hex_dump(pre["entry_dump"], pre["entry_addr"])
        print("  _LDR_DDAG_NODE dump (前 0x40 字节):", flush=True)
        hex_dump(pre["ddag_dump"], pre["ddag"])
    else:
        print("  卸载前未找到 injected.dll（异常）", flush=True)

    print("\n[4/5] 关闭 WT 窗口触发卸载...", flush=True)
    close_wt_window()

    # 等 mediator 退出
    try:
        mediator_proc.wait(timeout=5.0)
        print("  mediator 已退出", flush=True)
    except subprocess.TimeoutExpired:
        print("  mediator 超时未退出，kill", flush=True)
        mediator_proc.kill()

    # 等待 DLL 日志出现 "FreeLibrary" 或 "FreeLibraryAndExitThread"（确认 DoUnload 跑到末尾）
    dll_log = paths.injected_log(target_pid)
    print("\n[wait] 等待 DoUnload 完成（日志出现 FreeLibrary）...", flush=True)
    deadline = time.time() + 15.0
    unloaded_log_seen = False
    while time.time() < deadline:
        if os.path.exists(dll_log):
            with open(dll_log, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
                # 匹配 "FreeLibrary" 或 "FreeLibraryAndExitThread"
                if "FreeLibrary" in content and "returned" in content:
                    unloaded_log_seen = True
                    break
        time.sleep(0.3)

    if unloaded_log_seen:
        print("  DoUnload 已调用 FreeLibrary 并返回", flush=True)
    else:
        print("  超时未看到 FreeLibrary returned 日志", flush=True)

    # 再等几秒让 FreeLibrary 完成
    time.sleep(2.0)

    print("\n[5/5] 卸载后 LoadCount 检查（含字节 dump 对比）...", flush=True)
    post = find_injected_loadcount(target_pid, dump_bytes=True)
    if post:
        print("  卸载后: base_dll={base_dll} dll_base={dll_base:#x} "
              "loadcount={loadcount} obs={obs_loadcount} flags={flags:#x}".format(
                  **post))
        print("  entry_addr={:#x} ddag={:#x}".format(
            post["entry_addr"], post["ddag"]), flush=True)
        print("  _LDR_DATA_TABLE_ENTRY dump (前 0xA0 字节):", flush=True)
        hex_dump(post["entry_dump"], post["entry_addr"])
        print("  _LDR_DDAG_NODE dump (前 0x40 字节):", flush=True)
        hex_dump(post["ddag_dump"], post["ddag"])

        print("\n  *** DLL 仍在 PEB Ldr 链表 ***", flush=True)

        # 对比卸载前后的字节差异
        if pre and pre["entry_dump"] and post["entry_dump"]:
            print("\n  [字节差异] _LDR_DATA_TABLE_ENTRY:", flush=True)
            for i in range(min(len(pre["entry_dump"]), len(post["entry_dump"]))):
                if pre["entry_dump"][i] != post["entry_dump"][i]:
                    print("    +{:#x}: {:02x} → {:02x}".format(
                        i, pre["entry_dump"][i], post["entry_dump"][i]), flush=True)

        if pre and pre["ddag_dump"] and post["ddag_dump"]:
            print("\n  [字节差异] _LDR_DDAG_NODE:", flush=True)
            for i in range(min(len(pre["ddag_dump"]), len(post["ddag_dump"]))):
                if pre["ddag_dump"][i] != post["ddag_dump"][i]:
                    print("    +{:#x}: {:02x} → {:02x}".format(
                        i, pre["ddag_dump"][i], post["ddag_dump"][i]), flush=True)
    else:
        print("  卸载后 injected.dll 已从 PEB Ldr 链表消失", flush=True)
        print("  *** DLL 已真正卸载 ***", flush=True)

    # 读 DLL 日志末尾，确认 DETACH 是否触发
    if os.path.exists(dll_log):
        print("\n[DLL log] 末尾 25 行:", flush=True)
        with open(dll_log, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            for line in lines[-25:]:
                print("  " + line.rstrip(), flush=True)

    # 列出 cmd 进程的所有线程栈（看是否有线程仍在 DLL 代码中执行）
    print("\n[threads] cmd 进程线程列表（看是否有线程在 injected.dll 代码中）...", flush=True)
    try:
        import psutil
        p = psutil.Process(target_pid)
        for t in p.threads():
            print("  tid={} start_time={} ".format(t.id, t.start_time), flush=True)
    except Exception as e:
        print("  psutil 异常: {}".format(e), flush=True)

    # 不 cleanup cmd，让用户决定（保留现场供进一步调试）
    print("\n[done] cmd 进程 PID={} 仍在运行，供进一步调试".format(target_pid), flush=True)
    print("  手动清理: taskkill /F /PID {}".format(target_pid), flush=True)



if __name__ == "__main__":
    main()
