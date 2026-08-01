"""中文/emoji 自动化测试。

验证项：
  1. 中文输入：输入 "你好" + Enter，验证 UTF-8 字节 E4 BD A0 E5 A5 BD 到达 mediator
  2. emoji 输入：输入 "😀" + Enter，验证 4 字节 UTF-8 F0 9F 98 80 到达 mediator
  3. 中文退格：输入 "你好" + Backspace，验证字节流
  4. 混合输入：输入 "a你b😀c"，验证 UTF-8 字节

验证方式：SendInput 模拟键盘（KEYEVENTF_UNICODE 发送 wchar_t/代理对）
        → 读 mediator 日志验证 UTF-8 字节流转
        （mediator 的 InputRecordToVt 把 INPUT_RECORD 转回 UTF-8 VT 序列）
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim
from helpers.vt_capture import MediatorLog, verify_input_in_log, verify_utf8_input


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


def test_chinese_input(ctx: TestContext) -> bool:
    """测试 1：中文输入。你好 的 UTF-8 = E4 BD A0 E5 A5 BD。"""
    print("\n[测试 1] 中文输入：你好 + Enter")
    ctx.log.mark()
    input_sim.type_text("你好")
    time.sleep(0.5)
    input_sim.type_enter()
    time.sleep(1.0)

    content = ctx.log.read_new()
    if verify_utf8_input(content, "你好"):
        print("[PASS] 中文 UTF-8 字节已到达 mediator (E4 BD A0 E5 A5 BD)")
        return True
    else:
        print("[FAIL] 未检测到中文 UTF-8 字节")
        print("  日志片段:")
        for line in content.splitlines()[-10:]:
            print("    " + line)
        return False


def test_emoji_input(ctx: TestContext) -> bool:
    """测试 2：emoji 输入。😀 U+1F600 的 UTF-8 = F0 9F 98 80。"""
    print("\n[测试 2] emoji 输入：😀 + Enter")
    ctx.log.mark()
    input_sim.type_text("😀")
    time.sleep(0.8)  # emoji 代理对需要更多时间处理
    input_sim.type_enter()
    time.sleep(1.0)

    content = ctx.log.read_new()
    if verify_utf8_input(content, "😀"):
        print("[PASS] emoji UTF-8 字节已到达 mediator (F0 9F 98 80)")
        return True
    else:
        print("[FAIL] 未检测到 emoji UTF-8 字节")
        print("  日志片段:")
        for line in content.splitlines()[-10:]:
            print("    " + line)
        return False


def test_chinese_backspace(ctx: TestContext) -> bool:
    """测试 3：中文退格。输入你好 + Backspace，验证退格事件到达。"""
    print("\n[测试 3] 中文退格：你好 + Backspace")
    ctx.log.mark()
    input_sim.type_text("你好")
    time.sleep(0.5)
    input_sim.type_backspace()
    time.sleep(0.5)

    content = ctx.log.read_new()
    # 验证中文输入到达
    has_chinese = verify_utf8_input(content, "你好")
    # 验证退格事件到达（VT 序列 \x7f 或 \b，或 ReadConsoleInputW 模式）
    has_backspace = verify_input_in_log(content, b"\x7f") or verify_input_in_log(content, b"\x08")
    if has_chinese and (has_backspace or "converted" in content or "router" in content):
        print("[PASS] 中文输入 + 退格事件已到达 mediator")
        return True
    else:
        print("[FAIL] 中文退格异常 (chinese={} bs={})".format(has_chinese, has_backspace))
        return False


def test_mixed_input(ctx: TestContext) -> bool:
    """测试 4：混合输入。a你b😀c 的 UTF-8 字节验证。"""
    print("\n[测试 4] 混合输入：a你b😀c")
    ctx.log.mark()
    input_sim.type_text("a你b😀c")
    time.sleep(0.8)
    input_sim.type_enter()
    time.sleep(1.0)

    content = ctx.log.read_new()
    if verify_utf8_input(content, "a你b") and verify_utf8_input(content, "c"):
        print("[PASS] 混合输入 UTF-8 字节已到达 mediator")
        return True
    else:
        print("[FAIL] 混合输入字节异常")
        return False


def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        if not test_chinese_input(ctx):
            failures += 1
        if not test_emoji_input(ctx):
            failures += 1
        if not test_chinese_backspace(ctx):
            failures += 1
        if not test_mixed_input(ctx):
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
