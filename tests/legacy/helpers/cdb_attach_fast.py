"""启动注入 + 握手后立刻附加 cdb 抓取线程栈（非侵入式，抓完即分离）。

用法：python tests\helpers\cdb_attach_fast.py
输出：tests/helpers/cdb_fast_out.txt
"""
import os
import sys
import time
import subprocess
import glob

sys.path.insert(0, os.path.dirname(__file__))
from injector import start_target_cmd, start_wt_mediator, wait_for_handshake, clear_log

# 清旧日志
for f in glob.glob(r"C:\temp\injected_*.log"):
    try:
        os.remove(f)
    except OSError:
        pass

clear_log()

print("[1/4] 启动目标 cmd...", flush=True)
target_pid = start_target_cmd()
print("  cmd PID = {}".format(target_pid), flush=True)

print("[2/4] 启动 WT + mediator...", flush=True)
mediator_proc = start_wt_mediator(target_pid)

print("[3/4] 等待握手...", flush=True)
if not wait_for_handshake(timeout=20.0):
    print("[FATAL] 握手失败", flush=True)
    sys.exit(1)
print("  握手成功", flush=True)

# 握手后等 3 秒让内存泄漏积累（让线程进入稳定泄漏状态）
print("[4/4] 等 3s 让泄漏状态稳定后附加 cdb...", flush=True)
time.sleep(3.0)

# 附加 cdb，dump 线程栈，分离
TOOLS = r"c:\Users\rikka\Desktop\terminal-injector\.agents\skills\windows-debugging\10.0.19041.5609"
SYMSRV = "srv*e:\Symbol*http://msdl.blackint3.com:88/download/symbols"
IMGPATH = r"c:\Users\rikka\Desktop\terminal-injector\build\bin\Release"
OUT = r"c:\Users\rikka\Desktop\terminal-injector\tests\helpers\cdb_fast_out.txt"

# cdb 命令：~ 列线程；~*kp 全栈；查 g_initialized/g_initInProgress；qdetach 分离不杀进程
# 用 qd (quit detach) 让 cdb 分离进程，进程继续运行
cmd_str = '"{}" -p {} -y "{};{}" -i "{}" -logo "{}" -c "~;~*kp;?? g_initialized;?? g_initInProgress;qd"'
cmd_str = cmd_str.format(
    os.path.join(TOOLS, "cdb.exe"),
    target_pid,
    SYMSRV, IMGPATH,
    IMGPATH,
    OUT,
)
print("  运行 cdb: {}".format(cmd_str), flush=True)
ret = subprocess.run(cmd_str, shell=True, capture_output=True, text=True, timeout=60)
print("  cdb exit code: {}".format(ret.returncode), flush=True)
print("  cdb stdout tail:", flush=True)
print(ret.stdout[-1500:] if ret.stdout else "(empty)", flush=True)

# 读 cdb logo 文件大小
if os.path.exists(OUT):
    print("  cdb logo size: {} 字节".format(os.path.getsize(OUT)), flush=True)

# 检查 DLL 日志
log_path = r"C:\temp\injected_{}.log".format(target_pid)
if os.path.exists(log_path):
    print("  DLL 日志大小: {} 字节".format(os.path.getsize(log_path)), flush=True)

# 清理
print("\n清理进程...", flush=True)
import psutil
try:
    p = psutil.Process(target_pid)
    for child in p.children(recursive=True):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    p.terminate()
except psutil.NoSuchProcess:
    pass
mediator_proc.terminate()
try:
    mediator_proc.wait(timeout=3)
except Exception:
    mediator_proc.kill()
try:
    import win32gui, win32con
    from injector import _test_wt_hwnd
    if _test_wt_hwnd:
        win32gui.PostMessage(_test_wt_hwnd, win32con.WM_CLOSE, 0, 0)
except Exception:
    pass
print("完成", flush=True)
