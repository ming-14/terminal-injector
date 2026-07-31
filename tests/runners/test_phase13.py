"""Phase 13 VT 直通模式 e2e 测试。

验证项：
  1. ModeSwitchNotify 消息：DLL 检测到 VT_INPUT 标志变化时发送给 mediator
  2. VT 输出直通：ENABLE_VIRTUAL_TERMINAL_PROCESSING 开启时 WriteFile 原始字节直通

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - 在 cmd 中运行 Python 测试脚本（启用 VT 模式 + 写 VT 序列）
  - 读 mediator 日志，验证 ModeSwitchNotify 出现
  - 验证 VT 输出直通字节被正确转发

链路：
  python SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_INPUT)
  → DLL ModeHooks 检测 VT_INPUT 变化
  → 发 ModeSwitchNotify(vtInputMode=1) 给 mediator
  → mediator OnModeSwitchNotify 记录模式

  python WriteFile(stdout, "\x1b[31m...")
  → DLL OutputHooks 检测 ENABLE_VIRTUAL_TERMINAL_PROCESSING
  → 原始字节直通 SendToMediator → mediator pipe→stdout → WT 渲染
"""
import os
import sys
import re
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim
from helpers.vt_capture import MediatorLog


# Python 测试脚本路径（相对于 PROJECT_ROOT）
VT_TEST_SCRIPT = os.path.join("tests", "phase13_vt_test.py")


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


# ============================================================
# 测试 1：ModeSwitchNotify 消息验证
# ============================================================

def test_mode_switch_notify(ctx: TestContext) -> bool:
    """测试：DLL 检测到 VT_INPUT 变化时，ModeSwitchNotify 到达 mediator。

    验证方式：在 cmd 中运行 python 脚本，该脚本调用
    SetConsoleMode(stdin, ENABLE_VIRTUAL_TERMINAL_INPUT)。
    mediator 日志应出现 OnModeSwitchNotify。
    """
    print("\n[测试 1] ModeSwitchNotify：VT 输入模式切换通知")

    ctx.log.mark()

    # 输入 python 命令运行测试脚本
    cmd = "python {}".format(VT_TEST_SCRIPT)
    input_sim.type_text(cmd)
    time.sleep(0.4)
    input_sim.type_enter()

    # 等待 mediator 日志出现 OnModeSwitchNotify
    # 格式：OnModeSwitchNotify: VT input mode=1 (vtOutput=1)
    deadline = time.time() + 10.0
    found = False
    while time.time() < deadline:
        content = ctx.log.read_new()
        if "OnModeSwitchNotify" in content:
            found = True
            break
        time.sleep(0.3)

    if found:
        print("  [PASS] ModeSwitchNotify 已到达 mediator")
        return True
    else:
        # 打印日志帮助调试
        content = ctx.log.read_new()
        print("  [FAIL] 未检测到 OnModeSwitchNotify")
        # 打印最后 20 行日志
        lines = content.strip().split("\n")
        for line in lines[-20:]:
            print("  [LOG] {}".format(line))
        return False


# ============================================================
# 测试 2：VT 输出直通验证
# ============================================================

def test_vt_output_passthrough(ctx: TestContext) -> bool:
    """测试：VT 输出模式下 WriteFile 原始字节直通到 mediator。

    验证方式：python 脚本通过 WriteFile 直接写 VT 序列
    (\x1b[31mPhase13_VT_Passthrough\x1b[0m\n)，DLL 应直通
    这些字节到 mediator 的 ChildVtOutput 日志中。
    """
    print("\n[测试 2] VT 输出直通：WriteFile 原始字节直通")

    # 读完整日志（test 1 已用 read_new 读完，但内容还在日志中）
    content = ctx.log.read_all()
    passthrough_marker = b"Phase13_VT_Passthrough"
    hex_expected = " ".join("{:02X}".format(b) for b in passthrough_marker)
    if hex_expected in content:
        print("  [PASS] VT 输出直通字节已到达 mediator")
        return True
    if "Phase13_VT_Passthrough" in content:
        print("  [PASS] VT 输出直通字节已到达 mediator（原始字符串）")
        return True

    print("  [FAIL] 未检测到 VT 输出直通字节")
    # 打印最后 20 行帮助调试
    lines = content.strip().split("\n")
    for line in lines[-20:]:
        print("  [LOG] {}".format(line))
    return False


# ============================================================
# 测试 3：DLL 侧 ModeSwitchNotify 发送日志验证
# ============================================================

def test_dll_mode_switch_log(ctx: TestContext) -> bool:
    """测试：ChildSession RecvLoop 处理了 ModeSwitchNotify 消息。

    验证方式：mediator 日志应出现 "ChildSession RecvLoop: ModeSwitchNotify"
    （DLL 侧日志写入独立文件，不在 mediator 日志中）
    """
    print("\n[测试 3] ChildSession RecvLoop 处理 ModeSwitchNotify")

    content = ctx.log.read_all()
    if "ChildSession RecvLoop: ModeSwitchNotify" in content:
        print("  [PASS] ChildSession 已处理 ModeSwitchNotify")
        return True
    else:
        print("  [FAIL] 未检测到 ChildSession RecvLoop ModeSwitchNotify 日志")
        return False


# ============================================================
# 主入口
# ============================================================

def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        # 测试 1 和测试 2 在运行 python 脚本后共享日志，一起执行
        # 测试 3 检查日志中是否含 DLL 发送日志

        # 先运行测试脚本并等待结果
        if not test_mode_switch_notify(ctx):
            failures += 1

        # 等待 python 脚本执行完成（输出 VT 序列）
        time.sleep(2.0)

        if not test_vt_output_passthrough(ctx):
            failures += 1

        if not test_dll_mode_switch_log(ctx):
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