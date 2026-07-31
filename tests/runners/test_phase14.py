"""Phase 14 虚拟 Console 状态 e2e 测试。

验证项：
  1. VirtualConsoleState 初始化：DLL 日志出现 "VirtualConsoleState initialized"
  2. 光标位置设置 + 查询：SetConsoleCursorPosition 后 GetConsoleScreenBufferInfo 返回正确值
  3. WriteConsole 后光标推进：写文本后光标位置正确推进
  4. 文本属性设置 + 查询：SetConsoleTextAttribute 后查询返回正确属性
  5. WtStateReport resize：WT 窗口 resize 时 mediator 发送 WtStateReport

验证方式：
  - 启动 cmd + WT(mediator) + 注入
  - 在 cmd 中运行 Python 测试脚本（phase14_state_test.py）
  - 读 mediator 日志，验证 [STATE_TEST] 标记的 PASS/FAIL
  - 读 DLL 日志，验证 VirtualConsoleState 初始化
  - 调整 WT 窗口尺寸，验证 WtStateReport resize 日志
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim
from helpers.vt_capture import MediatorLog


# Python 测试脚本路径（相对于 PROJECT_ROOT）
STATE_TEST_SCRIPT = os.path.join("tests", "phase14_state_test.py")

# DLL 日志目录
DLL_LOG_DIR = r"C:\temp"


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

    def get_dll_log_path(self) -> str:
        """返回 DLL 日志路径（injected_<pid>.log）。"""
        return os.path.join(DLL_LOG_DIR, "injected_{}.log".format(self.target_pid))

    def search_child_output(self, marker: str) -> bool:
        """在 ChildVtOutput 日志的 hex 内容中搜索 marker 文本。

        因为 mediator 日志中 child VT 输出以 hex 形式记录，
        需要解码后再搜索。
        """
        content = self.read_all()
        # 提取所有 ChildVtOutput 行中的 hex 内容
        hex_pattern = re.compile(r"hex\[\d+\]=([0-9A-Fa-f ]+)")
        for m in hex_pattern.finditer(content):
            hex_str = m.group(1).strip()
            if hex_str:
                try:
                    decoded = bytes.fromhex(hex_str).decode("utf-8", errors="replace")
                    if marker in decoded:
                        return True
                except ValueError:
                    pass
        return False

    # 让 log 的 read_all 可直接访问
    def read_all(self) -> str:
        return self.log.read_all()


# ============================================================
# 测试 1：VirtualConsoleState 初始化验证
# ============================================================

def test_virtual_console_state_init(ctx: TestContext) -> bool:
    """测试：DLL 加载后 VirtualConsoleState 成功初始化。

    验证方式：检查 DLL 日志（injected_<pid>.log）中是否出现
    "VirtualConsoleState initialized" 字符串。
    """
    print("\n[测试 1] VirtualConsoleState 初始化验证")

    dll_log_path = ctx.get_dll_log_path()
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if os.path.exists(dll_log_path):
            try:
                with open(dll_log_path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    if "VirtualConsoleState initialized" in content:
                        print("  [PASS] VirtualConsoleState 已初始化")
                        return True
            except OSError:
                pass
        time.sleep(0.5)

    print("  [FAIL] 未检测到 VirtualConsoleState 初始化日志")
    print("  [INFO] 日志路径: {}".format(dll_log_path))
    if os.path.exists(dll_log_path):
        with open(dll_log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            for line in lines[-10:]:
                print("  [DLL LOG] {}".format(line.strip()))
    return False


# ============================================================
# 测试 2：光标位置设置 + 查询验证
# ============================================================

def test_cursor_set_and_query(ctx: TestContext) -> bool:
    """测试：SetConsoleCursorPosition 后 GetConsoleScreenBufferInfo 返回正确光标位置。

    验证方式：在 cmd 中运行 phase14_state_test.py，检查 mediator 日志中
    "[STATE_TEST] CURSOR_SET: PASS" 出现。
    """
    print("\n[测试 2] 光标位置设置 + 查询验证")

    ctx.log.mark()

    # 输入 python 命令运行测试脚本
    cmd = "python {}".format(STATE_TEST_SCRIPT)
    input_sim.type_text(cmd)
    time.sleep(0.4)
    input_sim.type_enter()

    # 等待测试完成（轮询搜索 hex 解码后的 marker）
    deadline = time.time() + 20.0
    found = False
    while time.time() < deadline:
        if ctx.search_child_output("[STATE_TEST] CURSOR_SET: PASS"):
            found = True
            break
        if ctx.search_child_output("[STATE_TEST] CURSOR_SET: FAIL"):
            print("  [FAIL] 光标位置设置验证失败")
            return False
        time.sleep(0.3)

    if found:
        print("  [PASS] 光标位置设置正确")
        return True
    else:
        content = ctx.log.read_all()
        print("  [FAIL] 未检测到 CURSOR_SET 结果")
        for line in content.split("\n")[-30:]:
            print("  [LOG] {}".format(line))
        return False


# ============================================================
# 测试 3：WriteConsole 后光标推进验证
# ============================================================

def test_cursor_advance(ctx: TestContext) -> bool:
    """测试：WriteConsole 后光标位置正确推进。

    验证方式：在 cmd 中运行 phase14_state_test.py（与测试 2 共享），
    检查 mediator 日志中 "[STATE_TEST] CURSOR_ADVANCE: PASS" 出现。
    """
    print("\n[测试 3] WriteConsole 后光标推进验证")

    # 测试 2 已经运行了脚本，检查 hex 解码后的日志中是否有 CURSOR_ADVANCE 结果
    if ctx.search_child_output("[STATE_TEST] CURSOR_ADVANCE: PASS"):
        print("  [PASS] 光标推进正确")
        return True
    if ctx.search_child_output("[STATE_TEST] CURSOR_ADVANCE: FAIL"):
        print("  [FAIL] 光标推进验证失败")
        return False

    print("  [FAIL] 未检测到 CURSOR_ADVANCE 结果")
    return False


# ============================================================
# 测试 4：文本属性设置 + 查询验证
# ============================================================

def test_attribute_set_and_query(ctx: TestContext) -> bool:
    """测试：SetConsoleTextAttribute 后查询返回正确属性值。

    验证方式：检查 mediator 日志中 "[STATE_TEST] ATTR_SET: PASS" 出现。
    """
    print("\n[测试 4] 文本属性设置 + 查询验证")

    content = ctx.log.read_all()
    if ctx.search_child_output("[STATE_TEST] ATTR_SET: PASS"):
        print("  [PASS] 文本属性设置正确")
        return True
    if ctx.search_child_output("[STATE_TEST] ATTR_SET: FAIL"):
        print("  [FAIL] 文本属性设置验证失败")
        return False

    print("  [FAIL] 未检测到 ATTR_SET 结果")
    return False


# ============================================================
# 测试 5：WtStateReport resize 验证
# ============================================================

def test_wt_state_report_resize(ctx: TestContext) -> bool:
    """测试：WT 窗口 resize 时 mediator 发送 WtStateReport。

    验证方式：通过 Win32 API 调整 WT 窗口尺寸，检查 mediator 日志
    中出现 "WtStateReport resize sent"。
    """
    print("\n[测试 5] WtStateReport resize 验证")

    import win32gui
    import win32con

    # 获取 WT 窗口句柄（使用 start_wt_mediator 记录的句柄）
    if injector._test_wt_hwnd is None:
        print("  [FAIL] 未找到 WT 窗口句柄")
        return False
    hwnd = injector._test_wt_hwnd

    ctx.log.mark()

    # 获取当前窗口尺寸并大幅缩小（确保 ConPTY 检测到变化）
    rect = win32gui.GetWindowRect(hwnd)
    cur_w = rect[2] - rect[0]
    cur_h = rect[3] - rect[1]
    new_w = max(600, cur_w - 200)  # 至少缩小 200px，最小 600px
    new_h = max(400, cur_h - 150)  # 至少缩小 150px，最小 400px

    # 调整窗口尺寸
    win32gui.SetWindowPos(hwnd, None, rect[0], rect[1], new_w, new_h,
                          win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)
    # 等待 WT 窗口处理 resize 消息
    time.sleep(1.0)

    # 恢复原尺寸（确保 resize 检测有足够的变化量）
    win32gui.SetWindowPos(hwnd, None, rect[0], rect[1], cur_w, cur_h,
                          win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE)

    # 等待 WtSizeWatcher 检测到变化并发送 WtStateReport
    deadline = time.time() + 10.0
    found = False
    while time.time() < deadline:
        content = ctx.log.read_new()
        if "WtStateReport resize sent" in content:
            found = True
            break
        time.sleep(0.3)

    if found:
        print("  [PASS] WtStateReport resize 已发送")
        return True
    else:
        content = ctx.log.read_all()
        print("  [FAIL] 未检测到 WtStateReport resize")
        for line in content.split("\n")[-20:]:
            print("  [LOG] {}".format(line))
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
        # 测试 1：VirtualConsoleState 初始化
        if not test_virtual_console_state_init(ctx):
            failures += 1

        # 测试 2-4：运行 Python 测试脚本并验证状态
        # 测试 2 会运行脚本，测试 3 和 4 共享同一次运行的日志
        if not test_cursor_set_and_query(ctx):
            failures += 1

        # 等待脚本继续执行（WriteConsole 和 SetConsoleTextAttribute）
        time.sleep(1.0)

        if not test_cursor_advance(ctx):
            failures += 1

        if not test_attribute_set_and_query(ctx):
            failures += 1

        # 测试 5：WtStateReport resize
        if not test_wt_state_report_resize(ctx):
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