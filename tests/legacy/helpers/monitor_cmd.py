"""监控 cmd 注入后的生命周期：内存增长、存活时间、退出码。

用法：python tests\helpers\monitor_cmd.py
"""
import os
import sys
import time
import subprocess

sys.path.insert(0, os.path.dirname(__file__))
from injector import start_target_cmd, start_wt_mediator, wait_for_handshake, clear_log

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import paths  # noqa: E402

import glob
for f in glob.glob(paths.injected_log_glob()):
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

# 监控 cmd 内存与存活
import psutil

print("[4/4] 监控 cmd 生命周期（每 0.5s 采样）...", flush=True)
log_path = paths.injected_log(target_pid)
samples = []
start = time.time()
try:
    p = psutil.Process(target_pid)
    while True:
        try:
            mem = p.memory_info()
            rss_mb = mem.rss / (1024 * 1024)
            alive = True
        except psutil.NoSuchProcess:
            alive = False
            rss_mb = 0
        # 读 DLL 日志大小
        log_size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
        elapsed = time.time() - start
        samples.append((elapsed, alive, rss_mb, log_size))
        print("  t={:6.1f}s alive={} rss={:.1f}MB dlllog={}B".format(
            elapsed, alive, rss_mb, log_size), flush=True)
        if not alive:
            print("  cmd 已退出！总存活时间 {:.1f}s".format(elapsed), flush=True)
            # 尝试获取退出码
            try:
                exit_code = p.wait(timeout=0.1)
                print("  退出码: {}".format(exit_code), flush=True)
            except Exception as e:
                print("  获取退出码失败: {}".format(e), flush=True)
            break
        time.sleep(0.5)
except KeyboardInterrupt:
    print("\n监控被中断", flush=True)

# 打印最终 DLL 日志内容
print("\n=== 最终 DLL 日志内容 ===", flush=True)
if os.path.exists(log_path):
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    print("大小: {} 字节".format(len(content)), flush=True)
    print("--- 内容 ---", flush=True)
    print(content, flush=True)
    print("--- 结束 ---", flush=True)
else:
    print("日志文件不存在", flush=True)

# 清理
print("\n清理进程...", flush=True)
try:
    import psutil
    pp = psutil.Process(target_pid)
    for child in pp.children(recursive=True):
        try:
            child.terminate()
        except psutil.NoSuchProcess:
            pass
    pp.terminate()
except psutil.NoSuchProcess:
    pass
mediator_proc.terminate()
try:
    mediator_proc.wait(timeout=3)
except Exception:
    mediator_proc.kill()

# 关闭 WT 窗口
try:
    import win32gui, win32con
    from injector import _test_wt_hwnd
    if _test_wt_hwnd:
        win32gui.PostMessage(_test_wt_hwnd, win32con.WM_CLOSE, 0, 0)
except Exception:
    pass

print("完成", flush=True)
