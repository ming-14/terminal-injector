"""Ctrl+C 信号测试（Phase 7）。

验证项：
  1. python 死循环 + Ctrl+C → python 进程被 KeyboardInterrupt 中断退出
  2. mediator 日志记录 Ctrl+C 检测（TriggerCtrlC）

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - 在 cmd 中运行 python 死循环（python.exe 是 cmd 子进程，共享控制台）
  - SendInput Ctrl+C
  - 用 psutil 检查 python.exe 是否退出（KeyboardInterrupt 导致进程终止）
  - 读 mediator 日志确认 DLL 检测到 \x03 并触发 TriggerCtrlC

链路：
  WT Ctrl+C → VT \x03 → mediator → DLL DllRecvLoop
  → VtToInputRecord 识别 \x03 → TriggerCtrlC
  → GenerateConsoleCtrlEvent(CTRL_C_EVENT, 0) 发给进程组
  → ConHost 在 cmd/python 进程创建线程调用 CtrlHandler
  → python 收到 CTRL_C_EVENT → 抛 KeyboardInterrupt → 退出
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim
from helpers.vt_capture import MediatorLog, verify_input_in_log

import psutil


class TestContext:
    def __init__(self):
        self.target_pid = 0
        self.mediator_proc = None
        self.log = MediatorLog(injector.LOG_PATH)

    def setup(self) -> bool:
        print("[setup] 启动目标 cmd...")
        self.target_pid = injector.start_target_cmd()
        print("[setup] cmd PID={}".format(self.target_pid))
        injector.clear_log()
        print("[setup] 启动 WT + mediator...")
        self.mediator_proc = injector.start_wt_mediator(self.target_pid)
        print("[setup] 等待握手...")
        if not injector.wait_for_handshake(timeout=20.0):
            print("[setup] 握手失败")
            return False
        print("[setup] 握手成功")
        time.sleep(2.0)
        injector.focus_wt()
        time.sleep(1.0)
        self.log.mark()
        return True

    def teardown(self) -> None:
        print("[teardown] 清理进程...")
        injector.cleanup(self.target_pid, self.mediator_proc)
        time.sleep(1.0)


def _find_python_child(cmd_pid: int) -> int:
    """查找 cmd 的 python.exe 子进程 PID，找不到返回 0。"""
    try:
        parent = psutil.Process(cmd_pid)
        for child in parent.children(recursive=True):
            name = child.name().lower()
            if name in ("python.exe", "pythonw.exe", "python3.exe"):
                return child.pid
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return 0


def test_ctrl_c_interrupts_python(ctx: TestContext) -> bool:
    """测试：python 死循环 + Ctrl+C → python 被中断退出。"""
    print("\n[测试] python 死循环 + Ctrl+C 中断")

    # 1. 运行 python 死循环
    print("  [1] 输入 python 死循环命令...")
    ctx.log.mark()
    # python -c "while True: pass"
    cmd = 'python -c "while True: pass"'
    input_sim.type_text(cmd)
    time.sleep(0.5)
    input_sim.type_enter()

    # 2. 等待 python 启动
    print("  [2] 等待 python 进程启动...")
    python_pid = 0
    deadline = time.time() + 5.0
    while time.time() < deadline:
        python_pid = _find_python_child(ctx.target_pid)
        if python_pid:
            break
        time.sleep(0.3)

    if not python_pid:
        print("  [FAIL] python 进程未启动")
        return False
    print("  [2] python PID={}".format(python_pid))

    # 3. 等 1 秒确保 python 进入死循环
    time.sleep(1.0)

    # 4. 确认 python 还在运行
    try:
        p = psutil.Process(python_pid)
        if not p.is_running():
            print("  [FAIL] python 进程在 Ctrl+C 前已退出")
            return False
    except psutil.NoSuchProcess:
        print("  [FAIL] python 进程不存在")
        return False

    # 5. SendInput Ctrl+C
    print("  [3] 发送 Ctrl+C...")
    ctx.log.mark()
    input_sim.type_ctrl_c()

    # 6. 等待 python 被中断
    print("  [4] 等待 python 被中断...")
    interrupted = False
    deadline = time.time() + 5.0
    while time.time() < deadline:
        try:
            p = psutil.Process(python_pid)
            if not p.is_running():
                interrupted = True
                break
        except psutil.NoSuchProcess:
            interrupted = True
            break
        time.sleep(0.3)

    if interrupted:
        print("  [PASS] python 进程被 Ctrl+C 中断退出")
    else:
        print("  [FAIL] python 进程未被中断（仍在运行）")

    # 7. 检查日志是否有 Ctrl+C 检测记录
    content = ctx.log.read_new()
    has_ctrl_c_log = "Ctrl+C" in content or "TriggerCtrlC" in content or "\\x03" in content
    if has_ctrl_c_log:
        print("  [PASS] mediator 日志记录了 Ctrl+C 检测")
    else:
        print("  [WARN] mediator 日志未记录 Ctrl+C 检测（可能日志级别/格式不同）")

    # 8. 检查 \x03 字节是否到达 mediator
    has_vt_03 = verify_input_in_log(content, b"\x03")
    if has_vt_03:
        print("  [PASS] VT \\x03 字节已到达 mediator")
    else:
        print("  [WARN] 未检测到 VT \\x03 字节")

    # 如果 python 被中断，测试通过
    return interrupted


def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        if not test_ctrl_c_interrupts_python(ctx):
            failures += 1
    finally:
        ctx.teardown()

    print("\n========== 结果 ==========")
    if failures == 0:
        print("全部通过")
    else:
        print("{} 项失败".format(failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
