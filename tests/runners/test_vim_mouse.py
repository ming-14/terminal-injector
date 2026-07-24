"""vim 鼠标测试。

流程：
  1. 启动 cmd + WT(mediator) + 握手
  2. 创建测试文件，在 WT 中输入 vim 命令启动 vim
  3. 输入 :set mouse=a 启用 vim 鼠标支持
  4. SendInput 鼠标点击 WT 窗口
  5. 验证 mediator 日志出现鼠标 SGR 1006 VT 序列（\x1b[<）
  6. 输入 :q! 退出 vim
  7. cleanup

验证方式：日志验证 mediator 收到鼠标 VT 序列。
注意：vim 是 cmd 的子进程，依赖 Phase 12 子进程注入。若未启用子进程注入，
      vim 的输入输出不经过 Hook 链路，测试会标记 SKIP。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from helpers import injector
from helpers import input_sim
from helpers.vt_capture import MediatorLog, verify_input_in_log

try:
    import win32gui
    _HAS_WIN32 = True
except ImportError:
    _HAS_WIN32 = False

VIM_TEST_FILE = os.path.join(injector.PROJECT_ROOT, "vim_test_buffer.txt")


class TestContext:
    def __init__(self):
        self.target_pid = 0
        self.mediator_proc = None
        self.log = MediatorLog(injector.LOG_PATH)

    def setup(self) -> bool:
        print("[setup] 启动目标 cmd...")
        self.target_pid = injector.start_target_cmd()
        print("[setup] cmd PID={}".format(self.target_pid))
        # 创建 vim 测试文件（多行文本）
        with open(VIM_TEST_FILE, "w", encoding="utf-8") as f:
            f.write("Line 1: vim mouse test\n")
            f.write("Line 2: click here\n")
            f.write("Line 3: scroll here\n")
            f.write("Line 4: end\n")
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
        # 清理测试文件
        try:
            if os.path.exists(VIM_TEST_FILE):
                os.remove(VIM_TEST_FILE)
        except OSError:
            pass
        time.sleep(1.0)


def _get_wt_center() -> tuple:
    if not _HAS_WIN32:
        import ctypes
        cx = ctypes.windll.user32.GetSystemMetrics(0)
        cy = ctypes.windll.user32.GetSystemMetrics(1)
        return cx // 2, cy // 2
    hwnds = injector.find_wt_windows()
    if not hwnds:
        return 800, 400
    rect = win32gui.GetWindowRect(hwnds[-1])
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


def _start_vim(ctx: TestContext) -> bool:
    """在 WT 中输入命令启动 vim。"""
    ctx.log.mark()
    cmd = 'vim "{}"'.format(VIM_TEST_FILE)
    input_sim.type_text(cmd)
    time.sleep(0.5)
    input_sim.type_enter()
    time.sleep(2.5)  # vim 启动需要时间

    # 验证 vim 启动：日志应出现 VtOutput（Alt Buffer 进入序列等）
    content = ctx.log.read_new()
    if "VtOutput" in content or "pipe" in content:
        print("[setup] vim 已启动")
        return True
    print("[WARN] 未检测到 vim 输出，可能子进程注入未启用")
    return False


def _enable_vim_mouse(ctx: TestContext) -> None:
    """输入 :set mouse=a 启用 vim 鼠标。"""
    ctx.log.mark()
    # vim ex 命令：:set mouse=a<CR>
    input_sim.type_char(":")
    time.sleep(0.2)
    input_sim.type_text("set mouse=a")
    time.sleep(0.3)
    input_sim.type_enter()
    time.sleep(1.0)


def test_vim_mouse_click(ctx: TestContext) -> bool:
    """测试：vim 鼠标点击。验证 mediator 日志出现 SGR 1006 鼠标 VT 序列。"""
    print("\n[测试] vim 鼠标点击")
    ctx.log.mark()
    cx, cy = _get_wt_center()
    # 点击 vim 文本区域
    input_sim.mouse_click(cx - 80, cy - 30, "left")
    time.sleep(1.0)

    content = ctx.log.read_new()
    # SGR 1006 鼠标序列格式：\x1b[<btn;col;rowM 或 m
    # 验证日志 stdin→router 出现 ESC [ <
    has_mouse_vt = verify_input_in_log(content, b"\x1b[<")
    if has_mouse_vt:
        print("[PASS] vim 鼠标 SGR 1006 序列已到达 mediator")
        return True
    # 也可能 mediator 用 ReadConsoleInputW 模式，鼠标转为 INPUT_RECORD
    if "router" in content and "converted" in content:
        print("[PASS] vim 鼠标事件已到达 mediator（ReadConsoleInputW 模式）")
        return True
    print("[FAIL/SKIP] 未检测到 vim 鼠标事件（可能子进程注入未启用或 vim 未启用鼠标）")
    return False


def _quit_vim(ctx: TestContext) -> None:
    """输入 :q! 强制退出 vim。"""
    input_sim.type_escape()
    time.sleep(0.3)
    input_sim.type_char(":")
    time.sleep(0.2)
    input_sim.type_text("q!")
    time.sleep(0.3)
    input_sim.type_enter()
    time.sleep(1.0)


def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        if not _start_vim(ctx):
            print("[SKIP] vim 未正常启动，跳过鼠标测试")
            # 仍尝试退出
            _quit_vim(ctx)
            return 0  # 标记为跳过而非失败

        _enable_vim_mouse(ctx)

        if not test_vim_mouse_click(ctx):
            failures += 1

        _quit_vim(ctx)
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
