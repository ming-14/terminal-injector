"""诊断脚本：用 cdb 监控 cmd 崩溃，捕获访问违例时的 full dump + callstack。

排查 Phase 11 验收 2 cmd 在 SendInput 后崩溃（exit_code=0xC0000005）。

流程：
  1. 启动 cmd + WT(mediator) + 注入
  2. 触发卸载（关闭 WT）
  3. 卸载完成后，启动 cdb -p <cmd_pid>，attach 到 cmd
     cdb 设置：sxe av（访问违例中断）+ 异常时 .dump /f + q
     cdb 后台运行，attach 后立即 g 让 cmd 继续运行
  4. SendInput 输入 echo 命令
  5. 等 cdb 退出（cmd 崩溃后 cdb 执行 dump + q）
  6. 用 cdb 分析 dump，打印 callstack

使用方法：
  python tests/helpers/diag_cmd_crash.py
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

import psutil

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from helpers import injector
from runners import test_phase11

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

# 工具路径
CDB_EXE = paths.cdb_exe()
SYMBOL_PATH = paths.symbol_path()

# dump 输出目录
DUMP_DIR = paths.injected_log_dir()
DUMP_PATH = os.path.join(DUMP_DIR, "cmd_crash.dmp")
CDB_LOG = os.path.join(DUMP_DIR, "cdb_cmd_monitor.log")


def start_cdb_monitor(cmd_pid: int) -> subprocess.Popen:
    """启动 cdb attach 到 cmd，监控访问违例异常。

    cdb 命令：
      -p <pid>           attach 到目标进程
      -y <symbol_path>   符号路径
      -c "sxe -c '...' av; g"   设置 av 异常 command + 立即 go
      -logo <log>        输出重定向到日志文件

    sxe -c "<cmd>" av：当 av 异常发生时执行 <cmd>
    <cmd> = .dump /f <path>; q   保存 full dump 后退出

    路径转义注意：
      cdb 的 .dump 命令解析路径时会把 \\t 解释为 tab，导致文件创建失败。
      必须用双反斜杠 \\\\ 传给 cdb，让 cdb 解析为单个反斜杠。

    返回 cdb 子进程对象。
    """
    # cdb .dump 路径需要双反斜杠（cdb 解析 \t 为 tab）
    # 但 dump 创建失败（HRESULT 0x80004005），改为打印 callstack + 寄存器 + 模块列表
    # kL 50: 打印当前线程 callstack（50 帧）
    # r: 打印寄存器
    # .ecxr: 切换到异常上下文（如果可用）
    # lm: 列出模块
    sxe_cmd = ".ecxr; r; kL 50; q"
    cdb_cmd = 'sxe -c "{}" av; g'.format(sxe_cmd)

    cmd = [
        CDB_EXE,
        "-p", str(cmd_pid),
        "-y", SYMBOL_PATH,
        "-c", cdb_cmd,
        "-logo", CDB_LOG,
    ]
    print("[cdb] 启动 cdb 监控: {}".format(" ".join(cmd)))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE,
    )
    return proc


def wait_cdb_attached(cdb_proc: subprocess.Popen, timeout: float = 15.0) -> bool:
    """等 cdb attach 完成（cdb 输出包含 'g' 命令提示）。

    cdb attach 后会暂停 cmd，执行 -c 命令（sxe + g），然后 cmd 继续。
    我们读取 cdb 输出，等到出现 "g" 或类似提示后认为 attach 完成。
    """
    start = time.time()
    attached_marker = b"0:000> sxe"  # cdb 提示符 + 我们执行的命令
    go_marker = b"g"  # 简单匹配，实际可能需要更精确

    # 非阻塞读取 cdb 输出
    import threading

    output_lines = []

    def reader():
        try:
            while True:
                line = cdb_proc.stdout.readline()
                if not line:
                    break
                output_lines.append(line)
                print("[cdb] {}".format(line.decode("utf-8", errors="ignore").rstrip()))
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    while time.time() - start < timeout:
        # 检查是否已执行 g 命令（attach 完成）
        full_output = b"".join(output_lines)
        if b"0:000> g" in full_output or b"> g" in full_output:
            print("[cdb] attach 完成，cmd 已继续运行")
            return True
        time.sleep(0.2)

    print("[cdb] attach 超时，继续执行（可能 cdb 输出格式不同）")
    return False


def wait_cdb_exit(cdb_proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    """等 cdb 退出（cmd 崩溃后 cdb 执行 dump + q）。

    返回 True 表示 cdb 已退出（dump 已生成）。
    """
    try:
        cdb_proc.wait(timeout=timeout)
        print("[cdb] cdb 已退出，exit_code={}".format(cdb_proc.returncode))
        return True
    except subprocess.TimeoutExpired:
        print("[cdb] cdb 超时未退出，强制 kill")
        cdb_proc.kill()
        try:
            cdb_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        return False


def analyze_dump(dump_path: str) -> None:
    """用 cdb 分析 dump，打印 callstack。"""
    if not os.path.exists(dump_path):
        print("[analyze] dump 文件不存在: {}".format(dump_path))
        return

    print("\n[analyze] 分析 dump: {}".format(dump_path))
    print("[analyze] dump 大小: {:,} bytes".format(os.path.getsize(dump_path)))

    # cdb -z <dump> -c "!analyze -v; k; q"
    cmd = [
        CDB_EXE,
        "-z", dump_path,
        "-y", SYMBOL_PATH,
        "-c", "!analyze -v; k; q",
    ]
    print("[analyze] 运行: {}".format(" ".join(cmd)))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    print("\n[analyze] cdb stdout:")
    print(proc.stdout)
    if proc.stderr:
        print("[analyze] cdb stderr:")
        print(proc.stderr)


def main():
    print("=" * 60)
    print("Phase 11 cmd 崩溃捕获诊断（cdb 监控）")
    print("=" * 60)
    print("dump 输出: {}".format(DUMP_PATH))
    print("cdb 日志: {}".format(CDB_LOG))

    # 清理旧 dump
    if os.path.exists(DUMP_PATH):
        os.remove(DUMP_PATH)
    if os.path.exists(CDB_LOG):
        os.remove(CDB_LOG)

    # 1. 启动 cmd
    print("\n[1] 启动 cmd...")
    cmd_pid = injector.start_target_cmd()
    print("    cmd PID={}".format(cmd_pid))

    # 2. 注入
    print("[2] 启动 WT + mediator...")
    injector.clear_log()
    mediator_proc = injector.start_wt_mediator(cmd_pid)
    print("[3] 等待握手...")
    if not injector.wait_for_handshake(timeout=20.0):
        print("[FATAL] 握手失败")
        injector.cleanup(cmd_pid, mediator_proc)
        return 1
    print("    握手成功")
    time.sleep(1.0)

    # 3. 先启动 cdb 监控（在卸载前 attach，全程监控卸载过程）
    #    原因：cmd 在卸载期间就可能崩溃，必须提前 attach
    print("\n[4] 启动 cdb 监控 cmd（卸载前 attach）...")
    cdb_proc = start_cdb_monitor(cmd_pid)
    wait_cdb_attached(cdb_proc, timeout=15.0)
    time.sleep(1.0)  # 给 cdb 额外时间稳定

    # 4. 触发卸载
    print("\n[5] 触发卸载...")
    # 注意：trigger_unload 内部会检查 cmd 存活状态，cdb attach 不影响存活检查
    unload_ok = test_phase11.trigger_unload(cmd_pid, mediator_proc, timeout=20.0)
    print("    卸载结果: {}".format("成功" if unload_ok else "失败"))

    # 5. SendInput 输入命令（复现崩溃）
    if unload_ok:
        print("\n[6] SendInput 输入 echo 命令...")
        from helpers import input_sim
        hwnd = test_phase11.find_console_window_by_pid(cmd_pid)
        if hwnd:
            user32 = ctypes.windll.user32
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.5)
            marker = "diag_crash_{}".format(int(time.time()) % 10000)
            input_sim.type_text("echo {}".format(marker))
            time.sleep(0.3)
            input_sim.type_enter()
            print("    已输入 echo {}".format(marker))
        else:
            print("  [WARN] 未找到 cmd console 窗口")

    # 6. 等 cdb 退出（cmd 崩溃后 cdb 执行 dump + q）
    print("\n[7] 等 cdb 捕获崩溃...")
    ok = wait_cdb_exit(cdb_proc, timeout=30.0)

    # 7. 分析结果（改为打印 cdb 日志中的 callstack）
    print("\n[8] 分析结果")
    if os.path.exists(CDB_LOG):
        print("\n--- cdb 日志（最后 200 行）---")
        with open(CDB_LOG, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            # 打印最后 200 行（包含崩溃 callstack）
            start = max(0, len(lines) - 200)
            print("".join(lines[start:]))
    else:
        print("[FAIL] cdb 日志不存在: {}".format(CDB_LOG))

    # 清理
    injector.cleanup(cmd_pid, None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
