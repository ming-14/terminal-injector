"""注入器辅助模块：启动目标 cmd + WT(mediator) + 窗口管理 + 清理。

测试流程：
  1. start_target_cmd() 启动注入目标 cmd，返回 PID
  2. start_wt_mediator(pid) 启动 WT 并在其中运行 mediator
  3. wait_for_handshake(pid) 等待握手成功
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

# 项目路径：与 common/paths.py 同一套相对解析（e2e 目录位置推导），
# 不硬编码机器路径；TI_PROJECT_ROOT 环境变量可覆盖
PROJECT_ROOT = os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
BUILD_BIN = os.path.join(PROJECT_ROOT, "build", "bin", "Release")
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
INJECTED_DLL = os.path.join(BUILD_BIN, "injected.dll")


def log_path(target_pid: int) -> str:
    """按目标 pid 定位 mediator 日志（与 DLL 侧 injected_<pid>.log 约定对齐）。

    mediator 按 pid 分日志文件（main.cpp Run()），并发会话互不干扰；
    握手扫描只匹配本会话日志，消除旧日志假阳性。
    """
    return os.path.join(BUILD_BIN, "terminal-injector-{}.log".format(target_pid))


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


def clear_log(target_pid: int) -> None:
    """清空该会话的 mediator 日志（测试前调用，确保 wait_for_handshake 匹配新日志）。"""
    try:
        path = log_path(target_pid)
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def wait_for_handshake(target_pid: int, timeout: float = 15.0) -> bool:
    """等待该会话 mediator 日志（terminal-injector-<pid>.log）出现 'Handshake OK'。

    按 pid 定位日志文件，只扫描本会话，不受其他会话/旧日志干扰。
    返回 True 表示成功，False 表示超时。
    """
    path = log_path(target_pid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
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

    注意（2026-08-05 全量回归实测）：wt.exe 是单实例进程——桌面上已有
    WT 窗口（如用户自己的）时，`wt -- cmd` 会**复用**窗口新开标签页，
    不产生新窗口，_test_wt_hwnd 检测失败，后续 focus 落到任意 WT，
    SendInput 打进错误窗口（READY 超时）。必须用 `-w <唯一名>` 强制
    每次新建独立窗口，保证窗口归属检测可靠。

    窗口名必须含时间戳：仅用 pid 时，Windows pid 复用会让 wt.exe 撞上
    旧测试残留的窗口名，改为**复用旧窗口**而不新建——新窗口检测不到，
    focus/cleanup 会误操作其他 WT 窗口（实测把用户窗口收到命令并关闭）。
    """
    global _test_wt_hwnd
    _test_wt_hwnd = None

    # 启动前快照已有 WT 窗口
    existing_hwnds = set(find_wt_windows()) if _HAS_WIN32 else set()

    # -w <时间戳唯一名>：绕过单实例复用与 pid 撞名，每次新建独立窗口
    win_name = "ti_e2e_{}_{}".format(target_pid, int(time.time() * 1000))
    wt_cmd = [find_wt_exe(), "-w", win_name, "--",
              MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)]
    proc = subprocess.Popen(wt_cmd)

    # 等待新 WT 窗口出现（最多 10 秒）
    if _HAS_WIN32:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            current_hwnds = set(find_wt_windows())
            new_hwnds = current_hwnds - existing_hwnds
            if new_hwnds:
                # 多窗口同时新增时（极端：用户恰好在同一时刻开 WT），
                # 优先选标题匹配测试窗口的（cmd/mediator 路径）；
                # 避免把用户窗口当测试窗口（后续 focus/cleanup 会误操作）
                matched = [h for h in new_hwnds
                           if _title_looks_test(h)]
                if len(new_hwnds) > 1 and not matched:
                    # 无法区分时等 0.5s 重试（测试窗口通常先出现）
                    time.sleep(0.5)
                    continue
                _test_wt_hwnd = sorted(matched or new_hwnds)[0]
                break
            time.sleep(0.3)

    return proc


def _title_looks_test(hwnd: int) -> bool:
    """窗口标题是否像测试窗口（cmd.exe / mediator 路径，而非用户窗口）。"""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
        t = buf.value.lower()
        return "cmd.exe" in t or "terminal_injector" in t
    except Exception:
        return False


def focus_wt(max_attempts: int = 5) -> Optional[int]:
    """把测试用 WT 窗口设为前台，供 SendInput 发送键盘鼠标。

    SetForegroundWindow 受 Windows 前台锁约束（快速连续会话/旧窗口关闭
    动画中可能被拒绝），因此必须验证前台切换是否真正生效：轮询
    GetForegroundWindow() 确认是目标窗口，失败重试；最终失败抛异常
    （明确失败而非静默丢输入——全量回归实测 READY 超时 90% 源于焦点
    未切换成功导致 SendInput 打空）。

    严禁兜底聚焦"任意 WT 窗口"：_test_wt_hwnd 未记录（检测失败）时
    直接抛异常——兜底会聚焦到用户自己的 WT 窗口，SendInput 把测试
    命令打进用户窗口、cleanup 还会把它关掉（2026-08-05 实测事故）。
    """
    if not _HAS_WIN32:
        return None
    import ctypes
    user32 = ctypes.windll.user32
    hwnd = _test_wt_hwnd
    if hwnd is None:
        raise RuntimeError(
            "no test WT window recorded (_test_wt_hwnd=None): "
            "refusing to focus arbitrary window")
    for _ in range(max_attempts):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        # 前台切换生效等待：轮询确认，覆盖异步拒绝/延迟
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if user32.GetForegroundWindow() == hwnd:
                return hwnd
            time.sleep(0.2)
        time.sleep(0.3)
    raise RuntimeError(
        "cannot focus WT window hwnd={}: foreground is {}".format(
            hex(hwnd), hex(user32.GetForegroundWindow())))


# 英文键盘布局的语言 ID（en-US 各变体）
_ENGLISH_LANGUAGE_IDS = (0x0409, 0x0809, 0x0C09, 0x1009)


def ensure_english_layout(max_attempts: int = 5) -> bool:
    """确保前台窗口使用英文键盘布局，绕过中文 IME 组词拦截。

    背景（2026-08-05）：系统键盘布局中文（Preload 00000804）优先时，
    中文 IME 激活会把 SendInput 的 VK 字母键截走组词，WT 收不到字符
    （keyboard 测试 EVENT_COUNT=0）。WT 是 XAML 窗口，ImmGetContext 返回 0，
    ImmSetOpenStatus 无法关闭其 IME（旧 disable_ime 失效），只能通过系统
    语言切换快捷键 Win+Space 轮询切到英文布局。

    副作用：切换后系统输入法停留在英文布局，测试不恢复（测试前后均以
    SendInput 模拟输入，不依赖用户输入法状态）。
    """
    if not _HAS_WIN32:
        return False
    import ctypes
    from helpers import input_sim
    user32 = ctypes.windll.user32

    for _ in range(max_attempts):
        # Win11 语言切换面板（XAML 瞬态窗口）会暂时成为前台，等待其消失
        fg = user32.GetForegroundWindow()
        cls = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(fg, cls, 128)
        if "XamlExplorerHostIslandWindow" in cls.value:
            time.sleep(1.0)
            continue
        tid = user32.GetWindowThreadProcessId(fg, None)
        hkl = user32.GetKeyboardLayout(tid)
        if hkl & 0xFFFF in _ENGLISH_LANGUAGE_IDS:
            return True
        # 模拟 Win+Space（系统级快捷键，不依赖前台窗口所属线程）
        input_sim.press_combo([input_sim.VK_LWIN, input_sim.VK_SPACE])
        time.sleep(0.9)
    return False


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
                # 等待窗口真正关闭（残留窗口会让下次 start_wt_mediator 的
                # 新增窗口检测/焦点归属错乱，全量实测曾累积到 3 个 WT 并存）
                deadline = time.time() + 5.0
                while time.time() < deadline and win32gui.IsWindow(_test_wt_hwnd):
                    time.sleep(0.2)
                if win32gui.IsWindow(_test_wt_hwnd):
                    win32gui.PostMessage(_test_wt_hwnd, win32con.WM_CLOSE, 0, 0)
                    time.sleep(1.0)
        except Exception:
            pass
        _test_wt_hwnd = None
        time.sleep(0.5)


def get_injection_command(target_pid: int) -> str:
    """返回注入命令字符串（供手动测试时显示给用户）。"""
    return f'{MEDIATOR_EXE} --mediator --target-pid {target_pid}'
