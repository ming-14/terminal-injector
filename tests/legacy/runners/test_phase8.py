"""Phase 8 高级特性测试。

验证项：
  1. SetConsoleTitleW Hook：cmd `title` 命令 → mediator 收到 OSC 序列 \x1b]0;<title>\x07
  2. GetConsoleTitleW Hook：cmd `title` 设置后，标题缓存可读（间接验证）

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - 在 cmd 中运行 `title <name>` 命令
  - 读 mediator 日志，解析 VtOutput 的 hex 字段
  - 验证 OSC 序列字节（\x1b]0; + title UTF-8 + \x07）出现

链路：
  cmd `title X` → cmd 调用 SetConsoleTitleW("X")
  → DLL Hook 拦截 → 缓存到 ConsoleState + 转 UTF-8 OSC 序列
  → SendToMediator → mediator pipe→stdout → WT 渲染标签页标题
"""
import os
import sys
import re
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim
from helpers.vt_capture import MediatorLog


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


# mediator 日志中 VtOutput 行的 hex 字段解析
# 格式：pipe→stdout: VtOutput len=N written=N ok=1 err=0 hex[N]=XX XX XX ... ...
_VT_OUTPUT_HEX_RE = re.compile(
    r"pipe.*stdout: VtOutput len=(\d+) written=\d+ ok=\d+ err=\d+ hex\[\d+\]=((?:[0-9A-Fa-f]{2} )*)"
)


def parse_vt_output_bytes(log: str):
    """从 mediator 日志提取所有 VtOutput 消息的字节内容。

    返回 list[bytes]，每个元素是一条 VtOutput 消息的完整字节。
    hex 字段只记录前 32 字节，超过部分用 ... 表示，此处返回最多 32 字节。
    """
    result = []
    for m in _VT_OUTPUT_HEX_RE.finditer(log):
        hex_str = m.group(2).strip()
        if hex_str:
            byte_vals = [int(x, 16) for x in hex_str.split()]
            result.append(bytes(byte_vals))
    return result


def find_osc_title(outputs, title_text: str) -> bool:
    """验证 outputs 中是否存在 OSC 标题序列 \x1b]0;<title>\x07。

    title_text 为预期标题（Python str），转 UTF-8 后匹配。
    """
    expected = b"\x1b]0;" + title_text.encode("utf-8") + b"\x07"
    for data in outputs:
        if expected in data:
            return True
    return False


def test_set_console_title(ctx: TestContext) -> bool:
    """测试：cmd `title Phase8Test` → mediator 收到 OSC 序列。"""
    print("\n[测试] SetConsoleTitleW Hook：cmd title 命令")

    title_name = "Phase8Test"
    ctx.log.mark()

    # 输入 title 命令
    input_sim.type_text("title {}".format(title_name))
    time.sleep(0.4)
    input_sim.type_enter()

    # 等待 OSC 序列出现在 mediator 日志（cmd 执行 title 命令需要时间）
    deadline = time.time() + 5.0
    found = False
    while time.time() < deadline:
        content = ctx.log.read_new()
        outputs = parse_vt_output_bytes(content)
        if find_osc_title(outputs, title_name):
            found = True
            break
        time.sleep(0.3)

    if found:
        print("  [PASS] OSC 标题序列 \\x1b]0;{}\\x07 已到达 mediator".format(title_name))
        return True
    else:
        # 打印收到的 VtOutput 帮助调试
        content = ctx.log.read_new()
        outputs = parse_vt_output_bytes(content)
        print("  [FAIL] 未检测到 OSC 标题序列")
        print("  [DEBUG] 收到 {} 条 VtOutput 消息".format(len(outputs)))
        for i, data in enumerate(outputs[:8]):
            print("  [DEBUG]   [{}] {} ({!r})".format(
                i, data.hex(" "), data))
        return False


def test_set_console_title_unicode(ctx: TestContext) -> bool:
    """测试：cmd `title 中文标题` → mediator 收到含中文 UTF-8 的 OSC 序列。"""
    print("\n[测试] SetConsoleTitleW Hook：中文标题")

    title_name = "中文标题"
    ctx.log.mark()

    input_sim.type_text("title {}".format(title_name))
    time.sleep(0.4)
    input_sim.type_enter()

    deadline = time.time() + 5.0
    found = False
    while time.time() < deadline:
        content = ctx.log.read_new()
        outputs = parse_vt_output_bytes(content)
        if find_osc_title(outputs, title_name):
            found = True
            break
        time.sleep(0.3)

    if found:
        print("  [PASS] 中文 OSC 标题序列已到达 mediator（UTF-8 编码）")
        return True
    else:
        print("  [FAIL] 未检测到中文 OSC 标题序列")
        return False


def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        if not test_set_console_title(ctx):
            failures += 1
        if not test_set_console_title_unicode(ctx):
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
