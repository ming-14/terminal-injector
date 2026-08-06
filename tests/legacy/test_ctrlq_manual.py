"""手动测试 Ctrl+Q 退出 Textual 应用"""
import os
import sys
import time
import subprocess

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "helpers"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from injector import (
    start_target_cmd, start_wt_mediator, wait_for_handshake,
    focus_wt, clear_log, BUILD_BIN, MEDIATOR_EXE, PROJECT_ROOT,
    LOG_PATH,
)
import input_sim as sim
import paths  # noqa: E402

TEXTUAL_DEMO = os.environ.get("TEXTUAL_DEMO")
if not TEXTUAL_DEMO:
    raise RuntimeError("TEXTUAL_DEMO 未设置（请通过环境变量指定 textual_demo.py 路径）")

def cleanup(*pids):
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                          capture_output=True, timeout=5)
        except:
            pass
    subprocess.run(["taskkill", "/F", "/IM", "terminal_injector.exe"],
                   capture_output=True)

def type_ctrl_q():
    """Ctrl+Q"""
    ctrl_down = sim._make_key_input(sim.VK_CONTROL, 0, 0)
    q_down = sim._make_key_input(0x51, 0, 0)  # 'Q'
    q_up = sim._make_key_input(0x51, 0, sim.KEYEVENTF_KEYUP)
    ctrl_up = sim._make_key_input(sim.VK_CONTROL, 0, sim.KEYEVENTF_KEYUP)
    sim._send(ctrl_down)
    time.sleep(0.05)
    sim._send(q_down)
    time.sleep(0.05)
    sim._send(q_up)
    sim._send(ctrl_up)
    time.sleep(0.5)

def find_textual_pids():
    """查找所有运行 textual_demo.py 的 Python 进程 PID"""
    import psutil
    pids = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            name = proc.info['name'] or ''
            cmdline = proc.info.get('cmdline') or []
            if 'python' in name.lower() and any('textual_demo' in c for c in cmdline):
                pids.append(proc.info['pid'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return pids

def is_process_alive(pid):
    """检查进程是否还在运行"""
    import psutil
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

def main():
    print("=== Ctrl+Q 退出测试 ===")
    cleanup()

    # 1. 启动目标 cmd
    print("1. 启动目标 cmd...")
    target_pid = start_target_cmd()
    print(f"   Target PID: {target_pid}")
    time.sleep(1)

    # 2. 清空日志
    clear_log()

    # 3. 启动 WT + mediator
    print("2. 启动 WT + mediator...")
    wt_proc = start_wt_mediator(target_pid)
    print(f"   WT PID: {wt_proc.pid}")

    # 4. 等待握手
    print("3. 等待握手...")
    if not wait_for_handshake(timeout=15):
        print("   [FAIL] 握手超时")
        cleanup(target_pid, wt_proc.pid)
        return False
    print("   [OK] 握手成功")

    # 5. 聚焦 WT
    print("4. 聚焦 WT...")
    focus_wt()
    time.sleep(1)

    # 6. 在 cmd 中执行 python textual_demo.py
    print("5. 执行 python textual_demo.py...")
    sim.type_text(f"python \"{TEXTUAL_DEMO}\"")
    sim.type_enter()
    print("   命令已发送")

    # 7. 等待 Textual 启动
    print("6. 等待 Textual 启动 (8s)...")
    time.sleep(8)

    # 8. 查找 Textual Python 进程
    before_pids = find_textual_pids()
    print(f"   Textual Python PIDs: {before_pids}")

    if not before_pids:
        print("   [WARN] 未找到 Textual Python 进程")
        print("   继续发送 Ctrl+Q...")

    # 9. 发送 Ctrl+Q
    print("7. 发送 Ctrl+Q...")
    type_ctrl_q()
    time.sleep(2)

    # 9.5 检查进程状态（详细诊断）
    print("   [DIAG] 检查进程状态...")
    import psutil
    for pid in before_pids:
        try:
            p = psutil.Process(pid)
            print(f"   PID {pid}: running={p.is_running()} status={p.status()} name={p.name()}")
            print(f"   cmdline: {p.cmdline()}")
        except psutil.NoSuchProcess:
            print(f"   PID {pid}: NoSuchProcess (已退出)")
        except psutil.AccessDenied:
            print(f"   PID {pid}: AccessDenied")
        except Exception as e:
            print(f"   PID {pid}: {e}")

    # 10. 检查进程是否还在
    still_alive = [pid for pid in before_pids if is_process_alive(pid)]
    if still_alive:
        print(f"   [FAIL] Ctrl+Q 后进程仍在运行: {still_alive}")
        # 额外检查：列出所有 textual_demo 进程
        after_pids = find_textual_pids()
        print(f"   当前 textual_demo 进程: {after_pids}")
        # 额外等待后再检查
        print("   等待 5 秒后重新检查...")
        time.sleep(5)
        still_alive_2 = [pid for pid in before_pids if is_process_alive(pid)]
        if still_alive_2:
            print(f"   [FAIL] 5 秒后仍在运行: {still_alive_2}")
        else:
            print(f"   [OK] 5 秒后进程已退出")
            still_alive = []
    else:
        print(f"   [OK] Ctrl+Q 后所有进程已退出")

    # 11. 检查日志确认 Ctrl+Q 被路由到子进程
    print("8. 检查日志...")
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, 'r', encoding='utf-8', errors='replace') as f:
            log_content = f.read()
        if 'converted 1 bytes: 11' in log_content:
            print("   [OK] 日志确认 Ctrl+Q (0x11) 被接收")
        else:
            print("   [WARN] 日志中未找到 Ctrl+Q 记录")
        if 'routed to child' in log_content:
            print("   [OK] 日志确认 Ctrl+Q 路由到子进程")
        if 'OnChildExit' in log_content:
            print("   [OK] 日志确认子进程已退出")
    else:
        print("   [WARN] 日志文件不存在")

    # 清理
    print("9. 清理...")
    cleanup(target_pid, wt_proc.pid)
    for pid in before_pids:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                          capture_output=True)
        except:
            pass

    if still_alive:
        print("\n=== 测试结果: 失败 (进程未退出) ===")
        return False
    else:
        print("\n=== 测试结果: 通过 ===")
        return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)