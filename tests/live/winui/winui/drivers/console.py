"""Win32 控制台驱动（框架与驱动层）。

通过 ctypes 直接调用 kernel32 的 Console API：
  - WriteConsoleOutputW 双缓冲差量输出
  - ReadConsoleInputW 读取键盘/鼠标/窗口尺寸事件
  - SetConsoleScreenBufferSize / SetConsoleWindowInfo 实现全屏
本驱动不依赖任何第三方库，仅使用 Windows 系统 DLL。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import logging
from dataclasses import dataclass

logger = logging.getLogger("winui.console")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


# ---------------------------------------------------------------- 常量

STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11

# 输入模式
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_INSERT_MODE = 0x0020
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080

# 事件类型
KEY_EVENT = 0x0001
MOUSE_EVENT = 0x0002
WINDOW_BUFFER_SIZE_EVENT = 0x0004
MENU_EVENT = 0x0008
FOCUS_EVENT = 0x0010

# 鼠标事件标志
MOUSE_MOVED = 0x0001
DOUBLE_CLICK = 0x0002
MOUSE_WHEELED = 0x0004
MOUSE_HWHEELED = 0x0008

# 鼠标按键
FROM_LEFT_1ST_BUTTON_PRESSED = 0x0001
FROM_LEFT_2ND_BUTTON_PRESSED = 0x0004
FROM_LEFT_3RD_BUTTON_PRESSED = 0x0008
RIGHTMOST_BUTTON_PRESSED = 0x0002
WHEEL_DELTA = 120

# 控制键状态
RIGHT_ALT_PRESSED = 0x0001
LEFT_ALT_PRESSED = 0x0002
RIGHT_CTRL_PRESSED = 0x0004
LEFT_CTRL_PRESSED = 0x0008
SHIFT_PRESSED = 0x0010

# 颜色属性
FG_BLUE = 0x0001
FG_GREEN = 0x0002
FG_RED = 0x0004
FG_INTENSITY = 0x0008
BG_BLUE = 0x0010
BG_GREEN = 0x0020
BG_RED = 0x0040
BG_INTENSITY = 0x0080
COMMON_LVB_LEADING_BYTE = 0x0100
COMMON_LVB_TRAILING_BYTE = 0x0200
COMMON_LVB_REVERSE_VIDEO = 0x4000
COMMON_LVB_UNDERSCORE = 0x8000

# 虚拟键码
VK_BACK = 0x08
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_PRIOR = 0x21  # PageUp
VK_NEXT = 0x22  # PageDown
VK_END = 0x23
VK_HOME = 0x24
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_INSERT = 0x2D
VK_DELETE = 0x2E
VK_F1 = 0x70
VK_F24 = 0x87


# ---------------------------------------------------------------- 结构体

class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short),
        ("Top", ctypes.c_short),
        ("Right", ctypes.c_short),
        ("Bottom", ctypes.c_short),
    ]


class CHAR_INFO(ctypes.Structure):
    _fields_ = [("Char", wt.WCHAR), ("Attributes", wt.WORD)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", wt.WORD),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD),
    ]


class CONSOLE_CURSOR_INFO(ctypes.Structure):
    _fields_ = [("dwSize", wt.DWORD), ("bVisible", wt.BOOL)]


class KEY_EVENT_RECORD(ctypes.Structure):
    # BOOL 在 Win32 结构体中固定 4 字节，ctypes 默认按 4 对齐，与 MSVC 布局一致
    _fields_ = [
        ("bKeyDown", wt.BOOL),
        ("wRepeatCount", wt.WORD),
        ("wVirtualKeyCode", wt.WORD),
        ("wVirtualScanCode", wt.WORD),
        ("UnicodeChar", wt.WCHAR),
        ("dwControlKeyState", wt.DWORD),
    ]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition", COORD),
        ("dwButtonState", wt.DWORD),
        ("dwControlKeyState", wt.DWORD),
        ("dwEventFlags", wt.DWORD),
    ]


class WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
    _fields_ = [("dwSize", COORD)]


class MENU_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwCommandId", wt.UINT)]


class FOCUS_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bSetFocus", wt.BOOL)]


class INPUT_EVENT_UNION(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KEY_EVENT_RECORD),
        ("MouseEvent", MOUSE_EVENT_RECORD),
        ("WindowBufferSizeEvent", WINDOW_BUFFER_SIZE_RECORD),
        ("MenuEvent", MENU_EVENT_RECORD),
        ("FocusEvent", FOCUS_EVENT_RECORD),
    ]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", INPUT_EVENT_UNION)]


# ---------------------------------------------------------------- 函数原型

GetStdHandle = kernel32.GetStdHandle
GetStdHandle.restype = wt.HANDLE
GetStdHandle.argtypes = [wt.DWORD]

GetConsoleMode = kernel32.GetConsoleMode
GetConsoleMode.restype = wt.BOOL
GetConsoleMode.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]

SetConsoleMode = kernel32.SetConsoleMode
SetConsoleMode.restype = wt.BOOL
SetConsoleMode.argtypes = [wt.HANDLE, wt.DWORD]

GetConsoleScreenBufferInfo = kernel32.GetConsoleScreenBufferInfo
GetConsoleScreenBufferInfo.restype = wt.BOOL
GetConsoleScreenBufferInfo.argtypes = [wt.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]

SetConsoleScreenBufferSize = kernel32.SetConsoleScreenBufferSize
SetConsoleScreenBufferSize.restype = wt.BOOL
SetConsoleScreenBufferSize.argtypes = [wt.HANDLE, COORD]

SetConsoleWindowInfo = kernel32.SetConsoleWindowInfo
SetConsoleWindowInfo.restype = wt.BOOL
SetConsoleWindowInfo.argtypes = [wt.HANDLE, wt.BOOL, ctypes.POINTER(SMALL_RECT)]

GetLargestConsoleWindowSize = kernel32.GetLargestConsoleWindowSize
GetLargestConsoleWindowSize.restype = COORD
GetLargestConsoleWindowSize.argtypes = [wt.HANDLE]

SetConsoleTitleW = kernel32.SetConsoleTitleW
SetConsoleTitleW.restype = wt.BOOL
SetConsoleTitleW.argtypes = [wt.LPCWSTR]

SetConsoleCursorInfo = kernel32.SetConsoleCursorInfo
SetConsoleCursorInfo.restype = wt.BOOL
SetConsoleCursorInfo.argtypes = [wt.HANDLE, ctypes.POINTER(CONSOLE_CURSOR_INFO)]

SetConsoleCursorPosition = kernel32.SetConsoleCursorPosition
SetConsoleCursorPosition.restype = wt.BOOL
SetConsoleCursorPosition.argtypes = [wt.HANDLE, COORD]

SetConsoleTextAttribute = kernel32.SetConsoleTextAttribute
SetConsoleTextAttribute.restype = wt.BOOL
SetConsoleTextAttribute.argtypes = [wt.HANDLE, wt.WORD]

WriteConsoleOutputW = kernel32.WriteConsoleOutputW
WriteConsoleOutputW.restype = wt.BOOL
WriteConsoleOutputW.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(CHAR_INFO),
    COORD,
    COORD,
    ctypes.POINTER(SMALL_RECT),
]

ReadConsoleInputW = kernel32.ReadConsoleInputW
ReadConsoleInputW.restype = wt.BOOL
ReadConsoleInputW.argtypes = [
    wt.HANDLE,
    ctypes.POINTER(INPUT_RECORD),
    wt.DWORD,
    ctypes.POINTER(wt.DWORD),
]


def _winerror(what: str) -> OSError:
    """把 last-error 包装成带上下文信息的 OSError。"""
    code = ctypes.get_last_error()
    return OSError(code, f"{what} 失败 (GetLastError={code})")


# ---------------------------------------------------------------- 输入事件模型

@dataclass(frozen=True)
class RawKeyEvent:
    """原始按键事件。ch 为 None 表示无 Unicode 字符（纯功能键）。"""
    down: bool
    vk: int
    ch: str | None
    ctrl: bool
    shift: bool
    alt: bool


@dataclass(frozen=True)
class RawMouseEvent:
    x: int
    y: int
    moved: bool
    double_click: bool
    wheel: int          # 0 无滚轮；正/负为滚动方向（WHEEL_DELTA 倍数）
    button: str | None  # "left" / "right" / "middle" / None


@dataclass(frozen=True)
class RawResizeEvent:
    width: int
    height: int


# ---------------------------------------------------------------- 驱动

class ConsoleDriver:
    """Windows 控制台驱动：全屏模式、双缓冲输出、输入读取。"""

    def __init__(self) -> None:
        self._h_in = None
        self._h_out = None
        self._orig_in_mode = 0
        self._orig_out_attr = 0
        self._size = (0, 0)
        self._cursor_visible = False

    # ---- 生命周期 ----
    def init(self) -> None:
        """获取句柄并设置输入/输出模式，进入全屏 TUI 模式。"""
        self._h_in = GetStdHandle(STD_INPUT_HANDLE)
        self._h_out = GetStdHandle(STD_OUTPUT_HANDLE)
        if self._h_in in (None, wt.HANDLE(-1).value) or self._h_out in (None, wt.HANDLE(-1).value):
            raise OSError("无法获取标准控制台句柄（可能未运行在控制台内）")

        in_mode = wt.DWORD()
        if not GetConsoleMode(self._h_in, ctypes.byref(in_mode)):
            raise _winerror("GetConsoleMode(输入)")
        self._orig_in_mode = in_mode.value

        info = self._read_buffer_info()
        self._orig_out_attr = info.wAttributes

        # 关闭 QuickEdit（否则鼠标点击会冻结输入），启用鼠标与窗口尺寸事件。
        # 注意：不能设置 ENABLE_PROCESSED_INPUT——它会将 Ctrl+C 转为进程信号，
        # 使应用无法在输入队列中收到 ctrl+c 按键事件。
        new_mode = (
            ENABLE_WINDOW_INPUT
            | ENABLE_MOUSE_INPUT
            | ENABLE_EXTENDED_FLAGS
        )
        if not SetConsoleMode(self._h_in, new_mode):
            raise _winerror("SetConsoleMode(输入)")

        self._size = (info.srWindow.Right - info.srWindow.Left + 1,
                      info.srWindow.Bottom - info.srWindow.Top + 1)
        self.set_cursor(False)
        logger.info("控制台就绪 尺寸=%sx%s", *self._size)

    def restore(self) -> None:
        """恢复原始控制台状态（模式、光标、清屏）。退出前必须调用。"""
        if self._h_in is not None:
            SetConsoleMode(self._h_in, self._orig_in_mode)
        if self._h_out is not None:
            sbi = CONSOLE_SCREEN_BUFFER_INFO()
            if GetConsoleScreenBufferInfo(self._h_out, ctypes.byref(sbi)):
                self._clear_screen(sbi.wAttributes)
                self.set_cursor(False)
                SetConsoleCursorPosition(self._h_out, COORD(0, 0))
                SetConsoleTextAttribute(self._h_out, self._orig_out_attr)

    def _clear_screen(self, attr: int) -> None:
        info = self._read_buffer_info()
        w = info.srWindow.Right - info.srWindow.Left + 1
        h = info.srWindow.Bottom - info.srWindow.Top + 1
        cells = (CHAR_INFO * (w * h))()
        for c in cells:
            c.Char = " "
            c.Attributes = attr
        rect = SMALL_RECT(0, 0, w - 1, h - 1)
        WriteConsoleOutputW(self._h_out, cells, COORD(w, h), COORD(0, 0), ctypes.byref(rect))

    # ---- 尺寸与全屏 ----
    def _read_buffer_info(self) -> CONSOLE_SCREEN_BUFFER_INFO:
        info = CONSOLE_SCREEN_BUFFER_INFO()
        if not GetConsoleScreenBufferInfo(self._h_out, ctypes.byref(info)):
            raise _winerror("GetConsoleScreenBufferInfo")
        return info

    def window_size(self) -> tuple[int, int]:
        """当前可见窗口大小 (宽, 高)（单元格）。"""
        info = self._read_buffer_info()
        return (info.srWindow.Right - info.srWindow.Left + 1,
                info.srWindow.Bottom - info.srWindow.Top + 1)

    def buffer_size(self) -> tuple[int, int]:
        """控制台缓冲区大小 (宽, 高)。"""
        info = self._read_buffer_info()
        return info.dwSize.X, info.dwSize.Y

    def set_window_size(self, width: int, height: int) -> None:
        """把窗口与缓冲区调整为恰好 (width, height)，隐藏滚动条，实现全屏铺满。

        顺序必须是：先缩小窗口 > 再改缓冲区 > 最后放大窗口。
        """
        if width < 1 or height < 1:
            raise ValueError(f"非法窗口尺寸 {width}x{height}")

        max_w, max_h = self.max_window_size()
        width, height = min(width, max_w), min(height, max_h)

        # 1) 窗口先缩到 1x1，避免缓冲区缩小失败
        sr = SMALL_RECT(0, 0, 0, 0)
        if not SetConsoleWindowInfo(self._h_out, True, ctypes.byref(sr)):
            raise _winerror("SetConsoleWindowInfo(缩小)")

        # 2) 调整缓冲区大小
        if not SetConsoleScreenBufferSize(self._h_out, COORD(width, height)):
            raise _winerror("SetConsoleScreenBufferSize")

        # 3) 窗口放大到全缓冲区
        sr = SMALL_RECT(0, 0, width - 1, height - 1)
        if not SetConsoleWindowInfo(self._h_out, True, ctypes.byref(sr)):
            raise _winerror("SetConsoleWindowInfo(放大)")

        self._size = (width, height)
        logger.info("窗口尺寸调整为 %sx%s", width, height)

    def max_window_size(self) -> tuple[int, int]:
        coord = GetLargestConsoleWindowSize(self._h_out)
        return coord.X, coord.Y

    # ---- 输出 ----
    def write(self, region: tuple[int, int, int, int],
              chars: list[list[tuple[str, int]]]) -> None:
        """把字符矩阵写入屏幕指定区域。

        region = (x, y, w, h)；chars 为 h 行、每行 w 个 (字符或 None, 属性)。
        字符为 None 表示原样保留屏幕内容（仅用于占位）。
        """
        x, y, w, h = region
        if w <= 0 or h <= 0:
            return
        total = w * h
        cells = (CHAR_INFO * total)()
        for row in range(h):
            line = chars[row]
            for col in range(w):
                idx = row * w + col
                ch, attr = line[col]
                cells[idx].Char = ch if ch is not None else " "
                cells[idx].Attributes = attr
        rect = SMALL_RECT(x, y, x + w - 1, y + h - 1)
        ok = WriteConsoleOutputW(self._h_out, cells, COORD(w, h), COORD(0, 0), ctypes.byref(rect))
        if not ok:
            raise _winerror("WriteConsoleOutputW")

    # ---- 光标 ----
    def set_cursor(self, visible: bool, pos: tuple[int, int] | None = None) -> None:
        info = CONSOLE_CURSOR_INFO(1, bool(visible))
        SetConsoleCursorInfo(self._h_out, ctypes.byref(info))
        if pos is not None:
            SetConsoleCursorPosition(self._h_out, COORD(*pos))
        self._cursor_visible = visible

    # ---- 标题 ----
    def set_title(self, title: str) -> None:
        SetConsoleTitleW(title)

    # ---- 输入 ----
    def read_input(self,
                   timeout_ms: int = -1
                   ) -> tuple[RawKeyEvent | RawMouseEvent | RawResizeEvent, ...]:
        """阻塞读取（timeout_ms=-1）或限时轮询控制台事件。

        内部用 PeekConsoleInputW 实现超时：先查有无事件，无则 Sleep 后重试。
        """
        deadline = None if timeout_ms < 0 else timeout_ms
        while True:
            peeked = wt.DWORD(0)
            pending = (INPUT_RECORD * 1)()
            if not kernel32.PeekConsoleInputW(self._h_in, pending, 1, ctypes.byref(peeked)):
                raise _winerror("PeekConsoleInputW")
            if peeked.value:
                return self._drain_input()
            if deadline is not None:
                sleep_ms = min(deadline, 5)
                ctypes.windll.kernel32.Sleep(sleep_ms)
                deadline -= sleep_ms
                if deadline <= 0:
                    return ()

    def _drain_input(self) -> tuple[RawKeyEvent | RawMouseEvent | RawResizeEvent, ...]:
        """一次性取出当前所有待处理事件（用 256 大小的缓冲循环读完）。"""
        events: list = []
        while True:
            buf = (INPUT_RECORD * 256)()
            read = wt.DWORD(0)
            if not ReadConsoleInputW(self._h_in, buf, 256, ctypes.byref(read)):
                raise _winerror("ReadConsoleInputW")
            count = read.value
            for rec in buf[:count]:
                ev = self._translate(rec)
                if ev is not None:
                    events.append(ev)
            if count < 256:
                break
        return tuple(events)

    def _translate(self, rec: INPUT_RECORD):
        e = rec.Event
        if rec.EventType == KEY_EVENT:
            k = e.KeyEvent
            if not k.bKeyDown:
                return None
            ch = k.UnicodeChar
            key = RawKeyEvent(
                down=True,
                vk=k.wVirtualKeyCode,
                ch=(ch if ch and ch != "\x00" else None),
                ctrl=bool(k.dwControlKeyState & (LEFT_CTRL_PRESSED | RIGHT_CTRL_PRESSED)),
                shift=bool(k.dwControlKeyState & SHIFT_PRESSED),
                alt=bool(k.dwControlKeyState & (LEFT_ALT_PRESSED | RIGHT_ALT_PRESSED)),
            )
            # Ctrl+字母 在 ReadConsoleInput 下 UnicodeChar 为控制字符，保留 vk 即可
            return key
        if rec.EventType == MOUSE_EVENT:
            m = e.MouseEvent
            if m.dwEventFlags & MOUSE_WHEELED:
                delta = ctypes.c_short(m.dwButtonState >> 16).value
                return RawMouseEvent(m.dwMousePosition.X, m.dwMousePosition.Y,
                                     False, False, delta // WHEEL_DELTA, None)
            button = None
            if m.dwButtonState & FROM_LEFT_1ST_BUTTON_PRESSED:
                button = "left"
            elif m.dwButtonState & RIGHTMOST_BUTTON_PRESSED:
                button = "right"
            elif m.dwButtonState & FROM_LEFT_2ND_BUTTON_PRESSED:
                button = "middle"
            elif m.dwButtonState & FROM_LEFT_3RD_BUTTON_PRESSED:
                button = "x1"
            return RawMouseEvent(
                m.dwMousePosition.X, m.dwMousePosition.Y,
                moved=bool(m.dwEventFlags & MOUSE_MOVED),
                double_click=bool(m.dwEventFlags & DOUBLE_CLICK),
                wheel=0,
                button=button,
            )
        if rec.EventType == WINDOW_BUFFER_SIZE_EVENT:
            return RawResizeEvent(e.WindowBufferSizeEvent.dwSize.X,
                                  e.WindowBufferSizeEvent.dwSize.Y)
        return None  # MENU / FOCUS 事件忽略