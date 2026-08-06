"""Phase 11 卸载诊断：检查 injected.dll 及其独占依赖的 LoadCount 变化。

假设：
  LoadCount 初始 5，FreeLibrary 减 3 → 剩 2。
  怀疑 LoadCount 包含"依赖关系引用"：
    - injected.dll 自己：1
    - 独占依赖（MSVCP140, VCRUNTIME140, VCRUNTIME140_1）：每个 +1
    - 其他内部引用：?

  FreeLibrary 时：
    - 减 injected 的 LoadCount 1 次
    - 尝试卸载独占依赖，每个卸载减 injected 的 LoadCount 1 次
    - 但某些依赖未卸载（LoadCount > 0），导致 injected 的 LoadCount 未归 0

本脚本：
  1. 启动 cmd + WT
  2. 卸载前查 injected + MSVCP140 + VCRUNTIME140 + VCRUNTIME140_1 的 LoadCount
  3. 关闭 WT 触发卸载
  4. 卸载后再次查这些 DLL 的 LoadCount
  5. 对比差异，定位引用持有者

不杀 cmd 进程（保留现场供进一步调试）。
不影响其他 WT 进程（只关闭自己创建的 WT 窗口）。

用法：python tests\helpers\diag_unload_deps.py
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
from diag_peb_loadcount import find_injected_loadcount, hex_dump  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402


# ============================================================
# 扩展：查找任意 DLL 的 LoadCount
# ============================================================
# 复用 diag_peb_loadcount 的 API 绑定
from diag_peb_loadcount import (  # noqa: E402
    _kernel32,
    _ntdll,
    PROCESS_BASIC_INFORMATION,
    ProcessBasicInformation,
    read_mem, read_ptr, read_ulong, read_ushort, read_unicode_string,
    OFF_DllBase, OFF_SizeOfImage, OFF_FullDllName, OFF_BaseDllName,
    OFF_Flags, OFF_ObsoleteLoadCount, OFF_DdagNode,
    OFF_DDAG_LoadCount, OFF_PEB_Ldr, OFF_LDR_InLoadOrderModuleList,
)


def find_module_loadcount(pid, name_pattern):
    """按名称查找模块（不区分大小写，支持部分匹配），返回 dict 或 None。"""
    hProc = _kernel32.OpenProcess(0x0400 | 0x0010, False, pid)  # QUERY_INFO | VM_READ
    if not hProc:
        return None
    try:
        pbi = PROCESS_BASIC_INFORMATION()
        retLen = wintypes.ULONG(0)
        status = _ntdll.NtQueryInformationProcess(
            hProc, ProcessBasicInformation, ctypes.byref(pbi),
            ctypes.sizeof(pbi), ctypes.byref(retLen))
        if status != 0:
            return None
        peb = pbi.PebBaseAddress
        if not peb:
            return None
        ldr = read_ptr(hProc, peb + OFF_PEB_Ldr)
        if not ldr:
            return None
        head_addr = ldr + OFF_LDR_InLoadOrderModuleList
        cur = read_ptr(hProc, head_addr)
        if not cur:
            return None
        name_pattern_lower = name_pattern.lower()
        max_iter = 512
        while cur and cur != head_addr and max_iter > 0:
            base_dll = read_unicode_string(hProc, cur + OFF_BaseDllName)
            if base_dll and name_pattern_lower in base_dll.lower():
                ddag = read_ptr(hProc, cur + OFF_DdagNode)
                real_loadcount = read_ulong(hProc, ddag + OFF_DDAG_LoadCount) if ddag else None
                obs = read_ushort(hProc, cur + OFF_ObsoleteLoadCount)
                flags = read_ulong(hProc, cur + OFF_Flags)
                dll_base = read_ptr(hProc, cur + OFF_DllBase)
                return {
                    "base_dll": base_dll,
                    "dll_base": dll_base or 0,
                    "loadcount": real_loadcount,
                    "obs_loadcount": obs or 0,
                    "flags": flags or 0,
                    "ddag": ddag or 0,
                    "entry_addr": cur,
                }
            cur = read_ptr(hProc, cur)
            max_iter -= 1
        return None
    finally:
        _kernel32.CloseHandle(hProc)


def find_module_loadcount_exact(pid, name):
    """按完整名称查找模块（不区分大小写），返回 dict 或 None。"""
    hProc = _kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not hProc:
        return None
    try:
        pbi = PROCESS_BASIC_INFORMATION()
        retLen = wintypes.ULONG(0)
        status = _ntdll.NtQueryInformationProcess(
            hProc, ProcessBasicInformation, ctypes.byref(pbi),
            ctypes.sizeof(pbi), ctypes.byref(retLen))
        if status != 0:
            return None
        peb = pbi.PebBaseAddress
        if not peb:
            return None
        ldr = read_ptr(hProc, peb + OFF_PEB_Ldr)
        if not ldr:
            return None
        head_addr = ldr + OFF_LDR_InLoadOrderModuleList
        cur = read_ptr(hProc, head_addr)
        if not cur:
            return None
        name_lower = name.lower()
        max_iter = 512
        while cur and cur != head_addr and max_iter > 0:
            base_dll = read_unicode_string(hProc, cur + OFF_BaseDllName)
            if base_dll and base_dll.lower() == name_lower:
                ddag = read_ptr(hProc, cur + OFF_DdagNode)
                real_loadcount = read_ulong(hProc, ddag + OFF_DDAG_LoadCount) if ddag else None
                obs = read_ushort(hProc, cur + OFF_ObsoleteLoadCount)
                flags = read_ulong(hProc, cur + OFF_Flags)
                dll_base = read_ptr(hProc, cur + OFF_DllBase)
                return {
                    "base_dll": base_dll,
                    "dll_base": dll_base or 0,
                    "loadcount": real_loadcount,
                    "obs_loadcount": obs or 0,
                    "flags": flags or 0,
                    "ddag": ddag or 0,
                    "entry_addr": cur,
                }
            cur = read_ptr(hProc, cur)
            max_iter -= 1
        return None
    finally:
        _kernel32.CloseHandle(hProc)


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


# 要检查的 DLL 列表（injected + 独占依赖）
TARGET_DLLS = [
    "injected.dll",
    "MSVCP140.dll",
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll",
    "ucrtbase.dll",  # API set 转发目标
    "KERNEL32.dll",
    "USER32.dll",
]


def snapshot_loadcounts(pid, label):
    """对 TARGET_DLLS 逐一查 LoadCount，返回 dict[name -> info]。"""
    print("\n[{}] 各 DLL LoadCount 快照 (pid={})".format(label, pid), flush=True)
    result = {}
    for name in TARGET_DLLS:
        info = find_module_loadcount_exact(pid, name)
        if info:
            print("  {:<25} LoadCount={:>3} obs={:>3} flags={:#x} base={:#x}".format(
                info["base_dll"], info["loadcount"], info["obs_loadcount"],
                info["flags"], info["dll_base"]), flush=True)
            result[name] = info
        else:
            print("  {:<25} (未找到)".format(name), flush=True)
            result[name] = None
    return result


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

    # 卸载前快照
    pre = snapshot_loadcounts(target_pid, "卸载前")

    print("\n[4/5] 关闭 WT 窗口触发卸载...", flush=True)
    close_wt_window()

    # 等 mediator 退出
    try:
        mediator_proc.wait(timeout=5.0)
        print("  mediator 已退出", flush=True)
    except subprocess.TimeoutExpired:
        print("  mediator 超时未退出，kill", flush=True)
        mediator_proc.kill()

    # 等待 DoUnload 完成
    dll_log = paths.injected_log(target_pid)
    print("\n[wait] 等待 DoUnload 完成...", flush=True)
    deadline = time.time() + 15.0
    unloaded_log_seen = False
    while time.time() < deadline:
        if os.path.exists(dll_log):
            with open(dll_log, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
                if "FreeLibrary" in content and "returned" in content:
                    unloaded_log_seen = True
                    break
        time.sleep(0.3)

    if unloaded_log_seen:
        print("  DoUnload 已调用 FreeLibrary", flush=True)
    else:
        print("  超时未看到 FreeLibrary returned 日志", flush=True)

    time.sleep(2.0)

    # 卸载后快照
    post = snapshot_loadcounts(target_pid, "卸载后")

    # 对比
    print("\n[对比] 卸载前后 LoadCount 变化:", flush=True)
    print("  {:<25} {:>15} {:>15} {:>10}".format("DLL", "pre.LoadCount", "post.LoadCount", "delta"),
          flush=True)
    for name in TARGET_DLLS:
        pre_info = pre.get(name)
        post_info = post.get(name)
        pre_lc = pre_info["loadcount"] if pre_info else "?"
        post_lc = post_info["loadcount"] if post_info else "?"
        if isinstance(pre_lc, int) and isinstance(post_lc, int):
            delta = post_lc - pre_lc
            delta_str = "{:+d}".format(delta)
        else:
            delta_str = "?"
        print("  {:<25} {:>15} {:>15} {:>10}".format(name, str(pre_lc), str(post_lc), delta_str),
              flush=True)

    # 读 DLL 日志末尾
    if os.path.exists(dll_log):
        print("\n[DLL log] 末尾 15 行:", flush=True)
        with open(dll_log, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            for line in lines[-15:]:
                print("  " + line.rstrip(), flush=True)

    # 不杀 cmd，保留现场
    print("\n[done] cmd 进程 PID={} 仍在运行，供进一步调试".format(target_pid), flush=True)
    print("  手动清理: taskkill /F /PID {}".format(target_pid), flush=True)



if __name__ == "__main__":
    main()
