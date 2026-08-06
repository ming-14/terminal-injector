"""Phase 11 诊断：卸载后用 cdb 附加，查看是否有线程仍在 injected.dll 代码中。

背景：
  FreeLibrary 调用后 LoadCount 从 5 减到 2（减 3），但依赖 DLL 无变化。
  怀疑有线程仍在 injected.dll 代码中执行（如卸载线程、worker、StatePoller 等），
  导致 LoadCount 无法归零，DETACH 未触发。

流程：
  1. 启动 cmd + WT，注入 DLL
  2. 关闭 WT 触发卸载
  3. 等 DoUnload 完成（FreeLibrary returned 日志出现）
  4. 用 cdb 附加，运行 ~*k（所有线程栈），然后 qd 退出
  5. 分析输出，统计 injected.dll 出现的栈帧

不杀 cmd 进程（保留现场）。不影响其他 WT 进程。

用法：python tests\helpers\diag_threads_after_unload.py
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

# cdb 路径（windows-debugging skill）
CDB_EXE = paths.cdb_exe()

# 符号路径（根据 project_memory 配置）
SYMBOL_PATH = paths.symbol_path()
# exe 路径（含 injected.dll 的 build/bin/Release）
EXE_PATH = paths.build_bin()


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
    if not os.path.exists(CDB_EXE):
        print("[FATAL] cdb.exe 不存在: {}".format(CDB_EXE), flush=True)
        sys.exit(1)

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

    print("\n[4/5] 关闭 WT 窗口触发卸载...", flush=True)
    close_wt_window()

    try:
        mediator_proc.wait(timeout=5.0)
        print("  mediator 已退出", flush=True)
    except subprocess.TimeoutExpired:
        print("  mediator 超时未退出，kill", flush=True)
        mediator_proc.kill()

    # 等 DoUnload 完成
    # Unloader 调用 Logger::Shutdown 后不再落盘日志，所以等待
    # "shutting down logger" 日志（Logger::Shutdown 前最后一行）
    dll_log = paths.injected_log(target_pid)
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

    # 等 ExitThread 完成（卸载线程退出）
    time.sleep(2.0)

    print("\n[5/5] 用 cdb 附加 PID={} 查看线程栈...".format(target_pid), flush=True)

    # cdb 命令：
    #   -p <pid> 附加
    #   -y 符号路径
    #   -i exe 路径（解析 injected.dll 符号）
    #   -logo 输出文件
    #   -c 命令：~*k（所有线程栈）+ lm（模块列表）+ qd（退出分离）
    log_file = os.path.join(paths.out_dir(), "cdb_threads_{}.log".format(target_pid))
    if os.path.exists(log_file):
        os.remove(log_file)

    # 用 ~*kf 显示完整栈（带帧数）
    # 用 lm m injected* 确认 DLL 仍在
    # .reload /f injected.dll 强制加载符号（避免 deferred 导致栈帧不显示符号）
    # !dlls 查看 LDR 链表详细信息（ReferenceCount、LoadCount、State）
    # !handle 查看进程句柄表（是否有指向 injected.dll 的句柄）
    cmds = (".reload /f injected.dll; "
            "~*kf; "
            "lm m injected*; "
            "!dlls -c injected.dll; "
            "!handle 0 7; "
            "qd")
    cmd_str = '"{}" -p {} -y "{};{}" -i "{}" -logo "{}" -c "{}"'.format(
        CDB_EXE, target_pid,
        SYMBOL_PATH, EXE_PATH,
        EXE_PATH, log_file,
        cmds
    )

    print("  cdb 命令: {}".format(cmd_str), flush=True)
    print("  附加中（cdb 会暂停目标进程）...", flush=True)

    try:
        result = subprocess.run(
            cmd_str, shell=True, capture_output=True, text=True, timeout=60.0
        )
        print("  cdb 退出码: {}".format(result.returncode), flush=True)
        if result.stderr:
            print("  cdb stderr: {}".format(result.stderr[:500]), flush=True)
    except subprocess.TimeoutExpired:
        print("  cdb 超时，强制终止", flush=True)
        # cdb 可能仍在运行，分离
        subprocess.run('taskkill /F /IM cdb.exe', shell=True, capture_output=True)

    # 读 cdb 日志
    if os.path.exists(log_file):
        print("\n  [cdb 日志] 读取 {}...".format(log_file), flush=True)
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            cdb_output = f.read()

        # 从 lm m injected* 输出提取 injected.dll 的 base 和 end 地址
        # 格式：00007ffe`16530000 00007ffe`165b0000   injected   (deferred)
        injected_base = None
        injected_end = None
        in_lm = False
        for line in cdb_output.splitlines():
            if "start             end                 module name" in line:
                in_lm = True
                continue
            if in_lm:
                if "injected" in line.lower():
                    parts = line.split()
                    if len(parts) >= 2:
                        # 解析 00007ffe`16530000 格式
                        base_str = parts[0].replace("`", "")
                        end_str = parts[1].replace("`", "")
                        try:
                            injected_base = int(base_str, 16)
                            injected_end = int(end_str, 16)
                        except ValueError:
                            pass
                    print("    " + line, flush=True)
                    in_lm = False
                elif line.strip() == "":
                    continue
                else:
                    in_lm = False

        if injected_base and injected_end:
            print("\n  [injected.dll 范围] {:#x} - {:#x} (size={:#x})".format(
                injected_base, injected_end, injected_end - injected_base), flush=True)
        else:
            print("\n  [警告] 未能从 lm 输出提取 injected.dll 地址范围", flush=True)

        # 分析：双重判断栈帧是否在 injected.dll 中
        # 1. 符号判断：找 "injected!" 开头的栈帧
        # 2. 地址判断：RetAddr 是否落在 injected.dll 范围内
        print("\n  [分析] 查找 injected.dll 中的栈帧（符号 + 地址双重判断）...", flush=True)
        injected_frames = []
        current_thread = -1
        import re
        # 栈帧行格式：偏移 Child-SP RetAddr Call Site
        # 例：a0 000000d0`23aff690 00007ffe`171e8b3f injected!terminjector::RingBufferLogger::WorkerMain+0x3d
        frame_re = re.compile(r"^\s*\w+\s+([0-9a-f`]+)\s+([0-9a-f`]+)\s+(.*)$")
        for line in cdb_output.splitlines():
            # 线程号标记：".  0  Id:..." 或 "#  0  Id:..."
            if " Id:" in line and ("." == line[0] or "#" == line[0]):
                try:
                    current_thread = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
            # 栈帧匹配
            m = frame_re.match(line)
            if m:
                ret_addr_str = m.group(2).replace("`", "")
                call_site = m.group(3)
                try:
                    ret_addr = int(ret_addr_str, 16)
                except ValueError:
                    continue
                # 符号判断
                has_symbol = "injected!" in call_site
                # 地址判断
                in_range = (injected_base is not None and
                           injected_end is not None and
                           injected_base <= ret_addr < injected_end)
                if has_symbol or in_range:
                    injected_frames.append((current_thread, line.strip(), "symbol" if has_symbol else "addr"))

        if injected_frames:
            print("  *** 发现 {} 个栈帧在 injected.dll 中 ***".format(
                len(injected_frames)), flush=True)
            for tid, frame, method in injected_frames:
                print("    线程 {} [{}]: {}".format(tid, method, frame), flush=True)
        else:
            print("  没有栈帧在 injected.dll 中（DLL 代码无活动线程）", flush=True)

        # 输出完整线程栈（前 200 行）
        print("\n  [完整线程栈] 前 200 行:", flush=True)
        for line in cdb_output.splitlines()[:200]:
            print("    " + line, flush=True)
    else:
        print("  cdb 日志未生成", flush=True)

    # 读 DLL 日志末尾
    if os.path.exists(dll_log):
        print("\n[DLL log] 末尾 15 行:", flush=True)
        with open(dll_log, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            for line in lines[-15:]:
                print("  " + line.rstrip(), flush=True)

    print("\n[done] cmd 进程 PID={} 仍在运行，供进一步调试".format(target_pid), flush=True)
    print("  手动清理: taskkill /F /PID {}".format(target_pid), flush=True)


if __name__ == "__main__":
    main()
