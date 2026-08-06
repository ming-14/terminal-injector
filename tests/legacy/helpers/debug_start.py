"""启动 cmd + mediator，握手后输出 PID，等待用户附加 cdb 调试。

用法：
  python tests\helpers\debug_start.py
  # 输出 cmd PID 后，用 cdb 附加：
  # cdb -p <cmd_pid> -y "<符号路径, 见 paths.symbol_path()>"
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

from injector import (start_target_cmd, start_wt_mediator, wait_for_handshake,
                       clear_log, focus_wt)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

# 清空旧日志
import glob
for f in glob.glob(paths.injected_log_glob()):
    try:
        os.remove(f)
    except OSError:
        pass

clear_log()

print("[1/4] 启动目标 cmd...")
target_pid = start_target_cmd()
print("  cmd PID = {}".format(target_pid))

print("[2/4] 启动 WT + mediator...")
mediator_proc = start_wt_mediator(target_pid)

print("[3/4] 等待握手...")
if not wait_for_handshake(timeout=20.0):
    print("[FATAL] 握手失败")
    sys.exit(1)
print("  握手成功")

print("[4/4] 等待 5 秒让 LazyInit 完成...")
time.sleep(5.0)

# 检查 DLL 日志
log_path = paths.injected_log(target_pid)
if os.path.exists(log_path):
    size = os.path.getsize(log_path)
    print("  DLL 日志: {} ({} bytes)".format(log_path, size))
    if size == 0:
        print("  [警告] DLL 日志为空！LazyInit 可能未完成或 Logger worker 未写入")
    else:
        print("  --- 日志前 20 行 ---")
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i >= 20:
                    break
                print("  " + line.rstrip())
else:
    print("  [警告] DLL 日志不存在: {}".format(log_path))

print()
print("=" * 60)
print("cmd PID = {}".format(target_pid))
print("mediator PID = {}".format(mediator_proc.pid))
print("=" * 60)
print()
print("现在可以附加 cdb 调试：")
print("  cdb -p {} -y \"srv*C:\\symbols*http://msdl.blackint3.com:88/download/symbols\"".format(target_pid))
print()
print("附加后，检查：")
print("  !analyze -v")
print("  ~                        # 列出线程")
print("  ~*s;kb                   # 每个线程调用栈")
print("  q                        # 退出 cdb")
print()
print("调试完成后，本脚本会一直保持进程不退出。")
print("如需手动清理，请关闭此脚本所在终端或运行：")
print("  Get-Process cmd,python,wt,terminal_injector | Stop-Process -Force")
print()
print("保持进程运行中... 按 Ctrl+C 退出并清理。")

# 保持进程运行，直到用户 Ctrl+C
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    pass

# cleanup
try:
    import psutil
    p = psutil.Process(target_pid)
    for child in p.children(recursive=True):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    p.terminate()
    p.wait(timeout=3)
except (psutil.NoSuchProcess, psutil.TimeoutExpired):
    pass

mediator_proc.terminate()
try:
    mediator_proc.wait(timeout=3)
except Exception:
    mediator_proc.kill()

print("清理完成")
