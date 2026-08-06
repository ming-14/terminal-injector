"""测试会话：启动 目标cmd + WT(mediator) + 注入 + 运行目标脚本 + 清理。

每个测试文件流程：
  1. TestSession() 上下文进入：启动 cmd、WT、握手、聚焦
  2. session.run_target(name, body) 生成并运行目标脚本
  3. session.wait_result(key) / 各驱动函数操作 WT
  4. 上下文退出：清理进程与窗口

复用 terminal-injector/tests/helpers 的 injector.py / input_sim.py。
"""
import os
import subprocess
import time

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from helpers import injector
from helpers import input_sim
from helpers import vt_capture

from . import paths
from . import result as result_mod
from . import target as target_mod


class TestSession:
    """一次注入会话（context manager）。

    用法：
        with TestSession() as s:
            s.run_target("t1", BODY)
            s.wait_result("t1", "OK")
            input_sim.type_text("hello")
    """

    def __init__(self, handshake_timeout: float = 20.0):
        self.handshake_timeout = handshake_timeout
        self.target_pid = 0
        self.mediator_proc = None
        self.entered = False

    # ---- 生命周期 ----
    def __enter__(self) -> "TestSession":
        print("[setup] 启动目标 cmd...")
        self.target_pid = injector.start_target_cmd()
        print("[setup] cmd PID={}".format(self.target_pid))
        injector.clear_log(self.target_pid)
        print("[setup] 启动 WT + mediator...")
        self.mediator_proc = injector.start_wt_mediator(self.target_pid)
        print("[setup] 等待握手...")
        if not injector.wait_for_handshake(self.target_pid, timeout=self.handshake_timeout):
            print("[setup] 握手失败")
            self.cleanup()
            raise RuntimeError("handshake failed")
        print("[setup] 握手成功")
        time.sleep(1.0)
        injector.focus_wt()
        time.sleep(0.5)
        # 确保英文键盘布局：中文 IME 会截走 SendInput 的 VK 字母键
        # （XAML WT 窗口 ImmGetContext 拿不到 IMC，只能系统级切布局）
        injector.ensure_english_layout()
        time.sleep(0.3)
        self.entered = True
        return self

    def cleanup(self) -> None:
        """清理测试进程与窗口。"""
        if self.target_pid:
            injector.cleanup(self.target_pid, self.mediator_proc)
            self.target_pid = 0
            self.mediator_proc = None
        time.sleep(0.5)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.entered:
            print("[teardown] 清理进程...")
            self.cleanup()
        return False

    # ---- 目标脚本 ----
    def _ensure_wt_foreground(self) -> None:
        """发送输入前确认 WT 在前台（SendInput 打给前台窗口）。

        focus_wt() 已做前台验证+重试，这里二次确认：前一个测试的 WT
        窗口关闭动画/系统弹窗可能重新抢占前台，导致命令全部打空。
        """
        if not injector._HAS_WIN32:
            return
        import ctypes
        user32 = ctypes.windll.user32
        if injector._test_wt_hwnd is not None:
            if user32.GetForegroundWindow() == injector._test_wt_hwnd:
                return
        injector.focus_wt()

    def run_target(self, name: str, body: str, ready_key: str = None,
                   ready_timeout: float = 20.0) -> None:
        """生成目标脚本并在注入 cmd 中运行。

        ready_key 非空时阻塞等待结果文件中出现该 KEY（脚本就绪）。
        超时抛 RuntimeError（含前台/结果文件诊断，定位输入丢失）。
        """
        result_mod.clear_result(name)
        script_path = target_mod.write_target(name, body)
        cmd = 'python "{}" "{}"'.format(script_path, result_mod.result_file(name))
        self._ensure_wt_foreground()
        input_sim.type_text(cmd)
        time.sleep(0.3)
        input_sim.type_enter()
        if ready_key:
            v = self.wait_result(name, ready_key, timeout=ready_timeout)
            if not v:
                import ctypes
                user32 = ctypes.windll.user32
                fg = user32.GetForegroundWindow()
                cls = ctypes.create_unicode_buffer(128)
                title = ctypes.create_unicode_buffer(512)
                user32.GetClassNameW(fg, cls, 128)
                user32.GetWindowTextW(fg, title, 512)
                print("  [DIAG] READY 超时: 前台=0x{:x} 类={} 标题={}".format(
                    fg, cls.value, title.value))
                print("  [DIAG] 结果文件内容: {}".format(
                    repr(result_mod.read_result(name))))
                raise RuntimeError(
                    "target script not ready: key={}".format(ready_key))
            time.sleep(0.5)

    def wait_result(self, name: str, key: str, timeout: float = 20.0) -> str:
        """等待目标脚本结果文件出现 key，返回 VALUE。"""
        return result_mod.wait_result(name, key, timeout=timeout)

    def wait_done(self, name: str, timeout: float = 20.0) -> bool:
        return result_mod.wait_done(name, timeout=timeout)

    # ---- mediator 日志（输出侧验证） ----
    def log(self) -> vt_capture.MediatorLog:
        return vt_capture.MediatorLog(paths.ti_log_path(self.target_pid))

    def log_tail(self, n: int = 15) -> None:
        """打印 mediator 日志末尾 n 行（失败时调试用）。"""
        try:
            content = self.log().read_all()
            for line in content.splitlines()[-n:]:
                print("  [LOG] {}".format(line))
        except OSError:
            print("  [LOG] (日志不可读)")

    # ---- WT 交互（键盘/鼠标驱动） ----
    def type_text(self, text: str) -> None:
        input_sim.type_text(text)

    def type_enter(self) -> None:
        input_sim.type_enter()

    def type_backspace(self) -> None:
        input_sim.type_backspace()

    def type_tab(self) -> None:
        input_sim.type_tab()

    def type_escape(self) -> None:
        input_sim.type_escape()

    def type_arrow(self, direction: str) -> None:
        input_sim.type_arrow(direction)

    def type_home(self) -> None:
        input_sim.type_home()

    def type_end(self) -> None:
        input_sim.type_end()

    def type_ctrl_c(self) -> None:
        input_sim.type_ctrl_c()

    def press_key(self, vk: int, hold: float = 0.05) -> None:
        input_sim.press_key(vk, hold=hold)

    def mouse_click(self, x: int, y: int, button: str = "left") -> None:
        input_sim.mouse_click(x, y, button)

    def mouse_wheel(self, x: int, y: int, delta: int = input_sim.WHEEL_DELTA) -> None:
        input_sim.mouse_wheel(x, y, delta)

    def mouse_hwheel(self, x: int, y: int, delta: int = input_sim.WHEEL_DELTA) -> None:
        input_sim.mouse_hwheel(x, y, delta)

    def mouse_move(self, x: int, y: int) -> None:
        input_sim.mouse_move(x, y)

    def mouse_drag(self, x1: int, y1: int, x2: int, y2: int,
                   button: str = "left", steps: int = 4,
                   step_sleep: float = 0.25) -> None:
        input_sim.mouse_drag(x1, y1, x2, y2, button=button,
                             steps=steps, step_sleep=step_sleep)

    def wt_rect(self) -> tuple:
        """WT 窗口屏幕矩形 (left, top, right, bottom)。

        只返回测试自己创建的窗口（_test_wt_hwnd）；未记录时返回 None，
        严禁兜底取任意 WT 窗口（鼠标点击会点到用户窗口，实测事故）。
        """
        try:
            import win32gui
            hwnd = injector._test_wt_hwnd
            if hwnd is None:
                return None
            return win32gui.GetWindowRect(hwnd)
        except Exception:
            pass
        return None

    def wt_center(self) -> tuple:
        """WT 窗口中心屏幕坐标（仅测试自建窗口，无则 None）。"""
        try:
            import win32gui
            hwnd = injector._test_wt_hwnd
            if hwnd is None:
                return None
            rect = win32gui.GetWindowRect(hwnd)
            return ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        except Exception:
            pass
        return None
