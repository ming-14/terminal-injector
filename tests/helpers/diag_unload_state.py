"""诊断新方案卸载后的 LDR State（不做远程 FreeLibrary 干扰现场）。

目的：
  新方案 CreateThread+FreeLibrary 后 LoadCount=1。
  判断这是延迟卸载状态（State=9 LdrModulesReadyToUnload，等待 LDR flush）
  还是真正的引用持有（State≠9，有线程在 DLL 代码中）。

不调用远程 FreeLibrary / LoadLibrary，保留现场供进一步调试（cdb 等）。
"""
import glob
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from injector import (  # noqa: E402
    clear_log,
    start_target_cmd,
    start_wt_mediator,
    wait_for_handshake,
)
from diag_peb_loadcount import (  # noqa: E402
    find_injected_loadcount,
    _kernel32,
    read_ulong,
    OFF_DDAG_State,
    OFF_DDAG_LowestLink,
    hex_dump,
)


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


def main():
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

    print("\n[4/5] 关闭 WT 窗口触发卸载...", flush=True)
    close_wt_window()

    try:
        mediator_proc.wait(timeout=5.0)
        print("  mediator 已退出", flush=True)
    except subprocess.TimeoutExpired:
        print("  mediator 超时未退出，kill", flush=True)
        mediator_proc.kill()

    # 等 DoUnload 完成（Logger::Shutdown 前最后一行日志）
    dll_log = r"C:\temp\injected_{}.log".format(target_pid)
    print("\n[wait] 等待 DoUnload 完成（shutting down logger 日志）...", flush=True)
    deadline = time.time() + 15.0
    unloaded_log_seen = False
    while time.time() < deadline:
        if os.path.exists(dll_log):
            with open(dll_log, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
                if "shutting down logger" in content:
                    unloaded_log_seen = True
                    break
        time.sleep(0.3)

    if unloaded_log_seen:
        print("  DoUnload 已调用 Logger::Shutdown", flush=True)
    else:
        print("  超时未看到 shutting down logger 日志", flush=True)

    # 等卸载线程退出 + 新线程 FreeLibrary 完成
    time.sleep(3.0)

    print("\n[5/5] 卸载后检查 LDR State（不做任何远程操作）...", flush=True)
    info = find_injected_loadcount(target_pid, dump_bytes=True)
    if info is None:
        print("  *** injected.dll 已从模块列表消失！新方案成功 ***", flush=True)
        print("\n[结论] CreateThread+FreeLibrary 方案有效，DLL 真正卸载", flush=True)
        return

    print("  injected.dll 仍在模块列表", flush=True)
    print("  base_dll={base_dll} dll_base={dll_base:#x} loadcount={loadcount} "
          "flags={flags:#x}".format(**info), flush=True)
    print("  entry_addr={:#x} ddag={:#x}".format(info["entry_addr"], info["ddag"]),
          flush=True)

    # 读 State 和 LowestLink
    hProc = _kernel32.OpenProcess(0x0410, False, target_pid)  # QUERY_INFO | VM_READ
    if hProc:
        state = read_ulong(hProc, info["ddag"] + OFF_DDAG_State)
        lowest_link = read_ulong(hProc, info["ddag"] + OFF_DDAG_LowestLink)
        print("\n  _LDR_DDAG_NODE.State = {} (9=LdrModulesReadyToUnload)".format(state),
              flush=True)
        print("  _LDR_DDAG_NODE.LowestLink = {}".format(lowest_link), flush=True)
        print("  _LDR_DDAG_NODE dump (前 0x40 字节):", flush=True)
        hex_dump(info["ddag_dump"], info["ddag"])
        _kernel32.CloseHandle(hProc)

    print("\n[结论] State={} 的解读:".format(state), flush=True)
    if state == 9:
        print("  State=9 (LdrModulesReadyToUnload)：DLL 已逻辑卸载，等待 LDR flush", flush=True)
        print("  LoadCount=1 是 LDR 延迟清理状态，不是真正引用持有", flush=True)
        print("  下次 Loader 操作（LoadLibrary/FreeLibrary）会触发清理", flush=True)
    else:
        print("  State={} 非 9：仍有真正引用持有，LoadCount=1 是引用计数".format(state),
              flush=True)
        print("  需用 cdb 附加查看哪些线程在 injected.dll 代码中", flush=True)

    print("\n[done] cmd 进程 PID={} 仍在运行，供 cdb 附加调试".format(target_pid),
          flush=True)
    print("  手动清理: taskkill /F /PID {}".format(target_pid), flush=True)


if __name__ == "__main__":
    main()
