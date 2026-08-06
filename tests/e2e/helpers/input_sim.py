"""输入模拟模块：用 SendInput 发送键盘/鼠标事件到前台 WT 窗口。

WT 接收键盘事件后转为 VT 序列发给 mediator stdin。
- 普通字符：VkKeyScanW 获取 VK 码
- Unicode 字符（中文/emoji）：KEYEVENTF_UNICODE + wScan
  - emoji 等 BMP 外字符：拆成高代理+低代理两个事件发送
- 特殊键：VK_RETURN/VK_BACK/VK_TAB/VK_ARROW 等
- 鼠标：MOUSEEVENTF_ABSOLUTE 归一化坐标

依赖：ctypes（无需 pywin32，直接调 user32）
"""
import ctypes
import time
from ctypes import wintypes
from typing import List, Optional

# SendInput 常量
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000

WHEEL_DELTA = 120

# 虚拟键码
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_HOME = 0x24
VK_END = 0x23
VK_PRIOR = 0x21  # PageUp
VK_NEXT = 0x22   # PageDown
VK_F1 = 0x70
VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_F5 = 0x74
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_F10 = 0x79
VK_F11 = 0x7A
VK_F12 = 0x7B
VK_LWIN = 0x5B


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("ki", KEYBDINPUT),
        ("mi", MOUSEINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("_input",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("_input", _INPUT_UNION),
    ]


_user32 = ctypes.windll.user32
_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
_user32.VkKeyScanW.restype = wintypes.SHORT

# GetSystemMetrics 用于屏幕分辨率归一化
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_SM_CXSCREEN = 0
_SM_CYSCREEN = 1


def _send(*inputs: INPUT) -> None:
    """发送一组 INPUT 结构。"""
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    _user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _make_key_input(vk: int, scan: int, flags: int) -> INPUT:
    """构建键盘 INPUT 结构。"""
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki.wVk = vk
    inp.ki.wScan = scan
    inp.ki.dwFlags = flags
    inp.ki.time = 0
    inp.ki.dwExtraInfo = ctypes.pointer(wintypes.ULONG(0))
    return inp


def press_key(vk: int, scan: int = 0, hold: float = 0.05) -> None:
    """按下并释放一个键。"""
    down = _make_key_input(vk, scan, 0)
    up = _make_key_input(vk, scan, KEYEVENTF_KEYUP)
    _send(down)
    time.sleep(hold)
    _send(up)
    time.sleep(0.02)


def type_char(ch: str) -> None:
    """输入单个字符（支持 Unicode：中文、emoji 代理对）。

    对 BMP 字符：直接用 KEYEVENTF_UNICODE 发送 wchar_t
    对 BMP 外字符（如 emoji）：拆成高代理+低代理两个事件
    """
    cp = ord(ch)
    if cp <= 0xFFFF:
        # BMP 字符：单次 Unicode 输入
        scan = cp & 0xFFFF
        down = _make_key_input(0, scan, KEYEVENTF_UNICODE)
        up = _make_key_input(0, scan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
        _send(down, up)
    else:
        # BMP 外字符：发送高代理 + 低代理
        cp -= 0x10000
        high = 0xD800 + (cp >> 10)
        low = 0xDC00 + (cp & 0x3FF)
        for surrogate in (high, low):
            scan = surrogate & 0xFFFF
            down = _make_key_input(0, scan, KEYEVENTF_UNICODE)
            up = _make_key_input(0, scan, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)
            _send(down, up)
            time.sleep(0.01)
    time.sleep(0.02)


def type_text(text: str) -> None:
    """输入字符串（逐字符，支持中文/emoji）。"""
    for ch in text:
        type_char(ch)


def type_enter() -> None:
    """按 Enter 键。"""
    press_key(VK_RETURN)


def type_backspace() -> None:
    """按 Backspace 键。"""
    press_key(VK_BACK)


def type_tab() -> None:
    """按 Tab 键。"""
    press_key(VK_TAB)


def type_escape() -> None:
    """按 Esc 键。"""
    press_key(VK_ESCAPE)


def type_arrow(direction: str) -> None:
    """按方向键：'up'/'down'/'left'/'right'。"""
    vk_map = {
        "up": VK_UP,
        "down": VK_DOWN,
        "left": VK_LEFT,
        "right": VK_RIGHT,
    }
    vk = vk_map.get(direction.lower())
    if vk is None:
        raise ValueError("direction must be up/down/left/right")
    press_key(vk)


def type_home() -> None:
    press_key(VK_HOME)


def type_end() -> None:
    press_key(VK_END)


def press_combo(vks, hold: float = 0.05) -> None:
    """按住多个键（顺序按下），再逆序释放。

    用于修饰键组合：press_combo([VK_CONTROL, 0x43]) = Ctrl+C
    注意：本函数不含延时分隔（Ctrl+C 信号场景需自加 sleep，见 type_ctrl_c）。
    """
    for vk in vks:
        _send(_make_key_input(vk, 0, 0))
        time.sleep(0.03)
    time.sleep(hold)
    for vk in reversed(vks):
        _send(_make_key_input(vk, 0, KEYEVENTF_KEYUP))
        time.sleep(0.03)
    time.sleep(0.02)


def type_ctrl_z() -> None:
    """Ctrl+Z：行编辑模式下触发 EOF。"""
    press_combo([VK_CONTROL, 0x5A])


def type_ctrl_c() -> None:
    """Ctrl+C：按住 Ctrl 按 C 再释放。"""
    ctrl_down = _make_key_input(VK_CONTROL, 0, 0)
    c_down = _make_key_input(0x43, 0, 0)  # 'C'
    c_up = _make_key_input(0x43, 0, KEYEVENTF_KEYUP)
    ctrl_up = _make_key_input(VK_CONTROL, 0, KEYEVENTF_KEYUP)
    _send(ctrl_down)
    time.sleep(0.05)
    _send(c_down)
    time.sleep(0.05)
    _send(c_up)
    _send(ctrl_up)
    time.sleep(0.05)


def _normalize_coords(x: int, y: int) -> tuple:
    """屏幕坐标归一化到 0-65535（SendInput 绝对坐标要求）。"""
    screen_w = _user32.GetSystemMetrics(_SM_CXSCREEN)
    screen_h = _user32.GetSystemMetrics(_SM_CYSCREEN)
    norm_x = int(x * 65535 / (screen_w - 1)) if screen_w > 1 else 0
    norm_y = int(y * 65535 / (screen_h - 1)) if screen_h > 1 else 0
    return norm_x, norm_y


def mouse_click(x: int, y: int, button: str = "left") -> None:
    """在屏幕坐标 (x, y) 点击鼠标。

    button: 'left'/'right'/'middle'
    """
    norm_x, norm_y = _normalize_coords(x, y)
    button = button.lower()
    if button == "left":
        down_flag, up_flag = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    elif button == "right":
        down_flag, up_flag = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    elif button == "middle":
        down_flag, up_flag = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP
    else:
        raise ValueError("button must be left/right/middle")

    move = INPUT()
    move.type = INPUT_MOUSE
    move.mi.dx = norm_x
    move.mi.dy = norm_y
    move.mi.mouseData = 0
    move.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    move.mi.time = 0
    move.mi.dwExtraInfo = ctypes.pointer(wintypes.ULONG(0))

    down = INPUT()
    down.type = INPUT_MOUSE
    down.mi.dx = norm_x
    down.mi.dy = norm_y
    down.mi.mouseData = 0
    down.mi.dwFlags = down_flag | MOUSEEVENTF_ABSOLUTE
    down.mi.time = 0
    down.mi.dwExtraInfo = ctypes.pointer(wintypes.ULONG(0))

    up = INPUT()
    up.type = INPUT_MOUSE
    up.mi.dx = norm_x
    up.mi.dy = norm_y
    up.mi.mouseData = 0
    up.mi.dwFlags = up_flag | MOUSEEVENTF_ABSOLUTE
    up.mi.time = 0
    up.mi.dwExtraInfo = ctypes.pointer(wintypes.ULONG(0))

    _send(move)
    time.sleep(0.05)
    _send(down)
    time.sleep(0.05)
    _send(up)
    time.sleep(0.05)


def mouse_wheel(x: int, y: int, delta: int = WHEEL_DELTA) -> None:
    """在 (x, y) 滚动滚轮。delta 正=上滚，负=下滚。"""
    norm_x, norm_y = _normalize_coords(x, y)
    move = INPUT()
    move.type = INPUT_MOUSE
    move.mi.dx = norm_x
    move.mi.dy = norm_y
    move.mi.mouseData = delta
    move.mi.dwFlags = MOUSEEVENTF_WHEEL | MOUSEEVENTF_ABSOLUTE
    move.mi.time = 0
    move.mi.dwExtraInfo = ctypes.pointer(wintypes.ULONG(0))
    _send(move)
    time.sleep(0.05)


def mouse_hwheel(x: int, y: int, delta: int = WHEEL_DELTA) -> None:
    """在 (x, y) 横向滚动滚轮。delta 正=右滚，负=左滚。"""
    norm_x, norm_y = _normalize_coords(x, y)
    move = INPUT()
    move.type = INPUT_MOUSE
    move.mi.dx = norm_x
    move.mi.dy = norm_y
    move.mi.mouseData = delta
    move.mi.dwFlags = MOUSEEVENTF_HWHEEL | MOUSEEVENTF_ABSOLUTE
    move.mi.time = 0
    move.mi.dwExtraInfo = ctypes.pointer(wintypes.ULONG(0))
    _send(move)
    time.sleep(0.05)


def mouse_move(x: int, y: int) -> None:
    """仅移动鼠标到 (x, y)，不按键。"""
    norm_x, norm_y = _normalize_coords(x, y)
    move = INPUT()
    move.type = INPUT_MOUSE
    move.mi.dx = norm_x
    move.mi.dy = norm_y
    move.mi.mouseData = 0
    move.mi.dwFlags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE
    move.mi.time = 0
    move.mi.dwExtraInfo = ctypes.pointer(wintypes.ULONG(0))
    _send(move)
    time.sleep(0.05)


def mouse_drag(x1: int, y1: int, x2: int, y2: int,
               button: str = "left", steps: int = 4, step_sleep: float = 0.25) -> None:
    """从 (x1,y1) 按住左键慢速拖到 (x2,y2) 再释放。

    steps 步进移动 + step_sleep 间隔：WT 需在按下期间采样到中间位置
    才发拖拽 SGR 序列（快速跳变会被 WT 合并为 down/up 两事件）。
    """
    norm1 = _normalize_coords(x1, y1)
    norm2 = _normalize_coords(x2, y2)
    if button == "left":
        down_flag, up_flag = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    elif button == "right":
        down_flag, up_flag = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    else:
        down_flag, up_flag = MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP

    def _mk(dx, dy, flags, data=0):
        i = INPUT()
        i.type = INPUT_MOUSE
        i.mi.dx = dx
        i.mi.dy = dy
        i.mi.mouseData = data
        i.mi.dwFlags = flags | MOUSEEVENTF_ABSOLUTE
        i.mi.time = 0
        i.mi.dwExtraInfo = ctypes.pointer(wintypes.ULONG(0))
        return i

    _send(_mk(norm1[0], norm1[1], MOUSEEVENTF_MOVE))
    time.sleep(0.1)
    _send(_mk(norm1[0], norm1[1], down_flag))
    time.sleep(step_sleep)
    for i in range(1, steps + 1):
        fx = norm1[0] + (norm2[0] - norm1[0]) * i // steps
        fy = norm1[1] + (norm2[1] - norm1[1]) * i // steps
        _send(_mk(fx, fy, MOUSEEVENTF_MOVE))
        time.sleep(step_sleep)
    _send(_mk(norm2[0], norm2[1], up_flag))
    time.sleep(0.1)
