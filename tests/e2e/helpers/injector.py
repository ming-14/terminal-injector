"""注入器辅助模块：启动目标 cmd + WT(mediator) + 窗口管理 + 清理。

测试流程：
  1. start_target_cmd() 启动注入目标 cmd，返回 PID
  2. start_wt_mediator(pid) 启动 WT 并在其中运行 mediator
  3. wait_for_handshake() 等待握手成功
  4. focus_wt() 把 WT 设为前台，供 SendInput 发送键盘鼠标
  5. cleanup() 终止测试进程

依赖：psutil, pywin32
"""
import os
import subprocess
import time
from typing import List, Optional

import psutil

try:
    import win32gui
    import win32con
    _HAS_WIN32 = True
except ImportError:
    _HAS_WIN32 = False

# 项目路径：优先读环境变量 TI_PROJECT_ROOT（e2e 在项目外，默认指向 terminal-injector）
_TI_DEFAULT_ROOT = r"C:\Users\rikka\Desktop\terminal-injector"
PROJECT_ROOT = os.environ.get("TI_PROJECT_ROOT") or _TI_DEFAULT_ROOT
BUILD_BIN = os.path.join(PROJECT_ROOT, "build", "bin", "Release")
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
INJECTED_DLL = os.path.join(BUILD_BIN, "injected.dll")
LOG_PATH = os.path.join(BUILD_BIN, "terminal-injector.log")


def start_target_cmd() -> int:
    """启动目标 cmd 进程（新控制台窗口），返回 PID。

    这个 cmd 是注入目标，注入后其输出被劫持到 mediator → WT。
    """
    proc = subprocess.Popen(
        ["cmd.exe"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        cwd=PROJECT_ROOT,
    )
    return proc.pid


def start_wt_mediator(target_pid: int) -> subprocess.Popen:
    """启动 WT 并在其中运行 mediator 注入命令。

    wt.exe -- <mediator> --mediator --target-pid <pid>
    WT 的 ConPTY 提供 mediator 的 stdin/stdout，符合架构。
    """
    wt_cmd = [find_wt_exe(), "--", MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)]
    proc = subprocess.Popen(wt_cmd)
    return proc


def find_wt_exe() -> str:
    """查找 wt.exe 路径（默认从 WindowsApps 解析）。"""
    # wt.exe 通常在 LOCALAPPDATA\Microsoft\WindowsApps
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    candidate = os.path.join(local_appdata, "Microsoft", "WindowsApps", "wt.exe")
    if os.path.exists(candidate):
        return candidate
    # 回退到 PATH 查找
    return "wt.exe"


def clear_log() -> None:
    """清空 mediator 日志（测试前调用，确保 wait_for_handshake 匹配新日志）。"""
    try:
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)
    except OSError:
        pass


def wait_for_handshake(timeout: float = 15.0) -> bool:
    """等待 mediator 日志出现 'Handshake OK'，表示注入握手成功。

    返回 True 表示成功，False 表示超时。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    if "Handshake OK" in content:
                        return True
                    # 注入失败也会记录日志，提前返回
                    if "Handshake failed" in content or "ERROR" in content:
                        return False
            except OSError:
                pass
        time.sleep(0.3)
    return False


def find_wt_windows() -> List[int]:
    """查找所有可见的 WT 窗口句柄。

    WT 窗口类名为 CASCADIA_HOSTING_WINDOW_CLASS。
    """
    if not _HAS_WIN32:
        return []
    result = []

    def _callback(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            class_name = win32gui.GetClassName(hwnd)
            if class_name == "CASCADIA_HOSTING_WINDOW_CLASS":
                result.append(hwnd)

    win32gui.EnumWindows(_callback, None)
    return result


# 测试启动的 WT 窗口句柄（由 start_wt_mediator 记录）
_test_wt_hwnd: Optional[int] = None


def start_wt_mediator(target_pid: int) -> subprocess.Popen:
    """启动 WT 并在其中运行 mediator 注入命令。

    wt.exe -- <mediator> --mediator --target-pid <pid>
    WT 的 ConPTY 提供 mediator 的 stdin/stdout，符合架构。

    启动前记录已有 WT 窗口，启动后找新增窗口存入 _test_wt_hwnd，
    供 focus_wt 精确聚焦（避免定位到其他遗留 WT 窗口）。
    """
    global _test_wt_hwnd
    _test_wt_hwnd = None

    # 启动前快照已有 WT 窗口
    existing_hwnds = set(find_wt_windows()) if _HAS_WIN32 else set()

    wt_cmd = [find_wt_exe(), "--", MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)]
    proc = subprocess.Popen(wt_cmd)

    # 等待新 WT 窗口出现（最多 10 秒）
    if _HAS_WIN32:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            current_hwnds = set(find_wt_windows())
            new_hwnds = current_hwnds - existing_hwnds
            if new_hwnds:
                # 取第一个新增窗口（通常只有一个）
                _test_wt_hwnd = sorted(new_hwnds)[0]
                break
            time.sleep(0.3)

    return proc


def focus_wt() -> Optional[int]:
    """把测试启动的 WT 窗口设为前台，返回窗口句柄。供 SendInput 使用。

    优先使用 start_wt_mediator 记录的 _test_wt_hwnd（精确锁定测试窗口）；
    若未记录（如 _HAS_WIN32=False），回退到最新的 WT 窗口。
    """
    if not _HAS_WIN32:
        return None
    hwnd = _test_wt_hwnd
    if hwnd is None:
        # 回退：取最新的 WT 窗口（不推荐，可能定位到其他 WT）
        hwnds = find_wt_windows()
        if not hwnds:
            return None
        hwnd = hwnds[-1]
    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.5)  # 等待前台切换生效
    return hwnd


def get_wt_window_rect() -> Optional[tuple]:
    """返回测试 WT 窗口的 (left, top, right, bottom)，供鼠标坐标计算用。

    鼠标测试用：点击坐标基于窗口客户区，而非屏幕绝对坐标。
    """
    if not _HAS_WIN32 or _test_wt_hwnd is None:
        return None
    return win32gui.GetWindowRect(_test_wt_hwnd)


def cleanup(target_pid: int, mediator_proc: Optional[subprocess.Popen] = None) -> None:
    """清理测试进程：终止目标 cmd、mediator、WT 窗口。

    遵循 project_memory 规则：自动终止测试遗留进程，无需询问用户。
    WT 窗口由 _test_wt_hwnd 指定（start_wt_mediator 记录），通过
    PostMessage(WM_CLOSE) 优雅关闭，避免遗留窗口干扰后续测试。
    """
    global _test_wt_hwnd

    # 终止目标 cmd（及其子进程）
    try:
        p = psutil.Process(target_pid)
        for child in p.children(recursive=True):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        p.terminate()
        p.wait(timeout=3)
    except (psutil.NoSuchProcess, psutil.TimeoutExpired):
        pass

    # 终止 mediator 进程
    if mediator_proc is not None:
        mediator_proc.terminate()
        try:
            mediator_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            mediator_proc.kill()

    # 清理可能残留的 terminal_injector 进程
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            name = proc.info["name"] or ""
            if name.lower() == "terminal_injector.exe":
                proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # 关闭测试启动的 WT 窗口（避免遗留窗口干扰后续测试/定位）
    if _HAS_WIN32 and _test_wt_hwnd is not None:
        try:
            if win32gui.IsWindow(_test_wt_hwnd):
                win32gui.PostMessage(_test_wt_hwnd, win32con.WM_CLOSE, 0, 0)
        except Exception:
            pass
        _test_wt_hwnd = None
        time.sleep(1.0)  # 等待 WT 窗口关闭完成


def get_injection_command(target_pid: int) -> str:
    """返回注入命令字符串（供手动测试时显示给用户）。"""
    return f'{MEDIATOR_EXE} --mediator --target-pid {target_pid}'
