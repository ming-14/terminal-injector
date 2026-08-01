"""python 鼠标 TUI 自检测试。

流程：
  1. 启动 cmd + WT(mediator) + 握手
  2. 在 WT 中输入命令运行 test_python_curses.py
  3. 等待程序就绪（结果文件出现 header）
  4. SendInput 鼠标点击 WT 窗口
  5. 读结果文件验证出现 MOUSE 事件
  6. 发送滚轮事件验证
  7. 发送 'q' 退出程序
  8. cleanup

验证方式：Python 目标程序用 ReadConsoleInputW 读取鼠标事件写入结果文件，
        测试脚本读文件验证事件到达。
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

TARGET_SCRIPT = os.path.join(injector.PROJECT_ROOT, "tests", "targets", "test_python_curses.py")
# 结果文件路径（与目标程序默认路径一致：cmd 的 cwd = PROJECT_ROOT）
RESULT_FILE = os.path.join(injector.PROJECT_ROOT, "mouse_result.txt")


class TestContext:
    def __init__(self):
        self.target_pid = 0
        self.mediator_proc = None
        self.log = MediatorLog(injector.LOG_PATH)

    def setup(self) -> bool:
        print("[setup] 启动目标 cmd...")
        self.target_pid = injector.start_target_cmd()
        print("[setup] cmd PID={}".format(self.target_pid))
        # 清理旧结果文件
        if os.path.exists(RESULT_FILE):
            os.remove(RESULT_FILE)
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


def _start_target_program(ctx: TestContext) -> bool:
    """在 WT 中输入命令启动 Python 鼠标测试程序。"""
    ctx.log.mark()
    # 用引号包裹路径（含空格安全）
    cmd = 'python "{}"'.format(TARGET_SCRIPT)
    input_sim.type_text(cmd)
    time.sleep(0.5)
    input_sim.type_enter()

    # 等待结果文件出现 header
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if os.path.exists(RESULT_FILE):
            try:
                with open(RESULT_FILE, "r", encoding="utf-8") as f:
                    if "# mouse test result" in f.read():
                        print("[setup] 目标程序已就绪")
                        return True
            except OSError:
                pass
        time.sleep(0.3)
    print("[FAIL] 目标程序启动超时")
    return False


def _get_wt_center() -> tuple:
    """获取 WT 窗口中心屏幕坐标。"""
    if not _HAS_WIN32:
        # 回退：屏幕中心
        import ctypes
        cx = ctypes.windll.user32.GetSystemMetrics(0)
        cy = ctypes.windll.user32.GetSystemMetrics(1)
        return cx // 2, cy // 2
    hwnds = injector.find_wt_windows()
    if not hwnds:
        return 800, 400
    rect = win32gui.GetWindowRect(hwnds[-1])
    cx = (rect[0] + rect[2]) // 2
    cy = (rect[1] + rect[3]) // 2
    return cx, cy


def test_mouse_click(ctx: TestContext) -> bool:
    """测试 1：鼠标点击。验证结果文件出现 MOUSE 事件。"""
    print("\n[测试 1] 鼠标点击")
    # 记录当前结果文件行数
    init_lines = 0
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            init_lines = sum(1 for _ in f)

    cx, cy = _get_wt_center()
    # 点击 WT 窗口中心稍偏左上（避开可能的滚动条）
    input_sim.mouse_click(cx - 100, cy - 50, "left")
    time.sleep(1.0)

    # 验证结果文件新增 MOUSE 行
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = [l for l in lines[init_lines:] if l.startswith("MOUSE")]
        if new_lines:
            print("[PASS] 鼠标点击事件已到达目标程序: {}".format(new_lines[0].strip()))
            return True
    print("[FAIL] 未检测到鼠标点击事件")
    return False


def test_mouse_wheel(ctx: TestContext) -> bool:
    """测试 2：滚轮滚动。验证结果文件出现 MOUSE 事件（含 WHEELED 标志）。"""
    print("\n[测试 2] 滚轮滚动")
    init_lines = 0
    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            init_lines = sum(1 for _ in f)

    cx, cy = _get_wt_center()
    # 上滚
    input_sim.mouse_wheel(cx, cy, input_sim.WHEEL_DELTA)
    time.sleep(0.5)
    # 下滚
    input_sim.mouse_wheel(cx, cy, -input_sim.WHEEL_DELTA)
    time.sleep(1.0)

    if os.path.exists(RESULT_FILE):
        with open(RESULT_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # MOUSE_WHEELED = 0x0004
        wheel_lines = [
            l for l in lines[init_lines:]
            if l.startswith("MOUSE") and " 4 " in l  # flags=4 表示 MOUSE_WHEELED
        ]
        if wheel_lines:
            print("[PASS] 滚轮事件已到达目标程序: {}".format(wheel_lines[0].strip()))
            return True
    print("[FAIL] 未检测到滚轮事件")
    return False


def _quit_target(ctx: TestContext) -> None:
    """发送 'q' 退出目标程序。"""
    input_sim.type_char("q")
    time.sleep(1.0)


def run() -> int:
    ctx = TestContext()
    if not ctx.setup():
        print("[FATAL] setup 失败")
        return 1

    failures = 0
    try:
        if not _start_target_program(ctx):
            failures += 1
            return failures

        if not test_mouse_click(ctx):
            failures += 1
        if not test_mouse_wheel(ctx):
            failures += 1

        _quit_target(ctx)
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
