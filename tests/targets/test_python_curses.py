"""鼠标事件自检目标程序（运行在被注入的 cmd 中）。

用 ReadConsoleInputW 直接读取控制台鼠标事件，验证鼠标 VT 翻译链路：
  WT 点击 → mediator → DLL VtToInputRecord → InputQueue → 本程序 ReadConsoleInputW

功能：
  1. 设置 ConsoleMode 启用 ENABLE_MOUSE_INPUT
  2. 循环 ReadConsoleInputW 读取事件
  3. 鼠标事件：记录坐标/按键/滚轮，写入结果文件 + 屏幕显示
  4. 键盘 'q'：退出

结果文件路径由环境变量 MOUSE_RESULT_FILE 指定，默认 ./mouse_result.txt
每行格式：MOUSE <x> <y> <button> <flags> <ctrlState>
           KEY <vk> <ch>
           QUIT

依赖：仅 ctypes（Python 3.8+，无需第三方包）
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

# Console API 常量
ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_EXTENDED_FLAGS = 0x0080
DISABLE_NEWLINE_AUTO_RETURN = 0x0008  # extended flag

KEY_EVENT = 0x0001
MOUSE_EVENT = 0x0002
WINDOW_BUFFER_SIZE_EVENT = 0x0004

FROM_LEFT_1ST_BUTTON_PRESSED = 0x0001
RIGHTMOST_BUTTON_PRESSED = 0x0002
FROM_LEFT_2ND_BUTTON_PRESSED = 0x0004
FROM_LEFT_3RD_BUTTON_PRESSED = 0x0008
FROM_LEFT_4TH_BUTTON_PRESSED = 0x0010

MOUSE_WHEELED = 0x0004
MOUSE_HWHEELED = 0x0008


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("dwMousePosition", COORD),
        ("dwButtonState", wintypes.DWORD),
        ("dwControlKeyState", wintypes.DWORD),
        ("dwEventFlags", wintypes.DWORD),
    ]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", wintypes.WCHAR),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _INPUT_RECORD_UNION(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KEY_EVENT_RECORD),
        ("MouseEvent", MOUSE_EVENT_RECORD),
    ]


class INPUT_RECORD(ctypes.Structure):
    _anonymous_ = ("_event",)
    _fields_ = [
        ("EventType", wintypes.WORD),
        ("_event", _INPUT_RECORD_UNION),
    ]


_kernel32 = ctypes.windll.kernel32
_kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
_kernel32.GetStdHandle.restype = wintypes.HANDLE
_kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetConsoleMode.restype = wintypes.BOOL
_kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.SetConsoleMode.restype = wintypes.BOOL
_kernel32.ReadConsoleInputW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
]
_kernel32.ReadConsoleInputW.restype = wintypes.BOOL
_kernel32.WriteConsoleW.argtypes = [
    wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p,
]
_kernel32.WriteConsoleW.restype = wintypes.BOOL

STD_INPUT_HANDLE = 0xFFFFFFF6
STD_OUTPUT_HANDLE = 0xFFFFFFF5


def main():
    result_file = os.environ.get("MOUSE_RESULT_FILE", "mouse_result.txt")
    h_in = _kernel32.GetStdHandle(STD_INPUT_HANDLE)
    h_out = _kernel32.GetStdHandle(STD_OUTPUT_HANDLE)

    # 设置输入模式：启用鼠标，禁用 line input/echo/quick-edit
    new_mode = (
        ENABLE_MOUSE_INPUT
        | ENABLE_EXTENDED_FLAGS  # 禁用 quick-edit 需要此标志
        | ENABLE_WINDOW_INPUT
    )
    if not _kernel32.SetConsoleMode(h_in, new_mode):
        print("SetConsoleMode failed: {}".format(ctypes.get_last_error()))
        return 1

    # 写结果文件头
    with open(result_file, "w", encoding="utf-8") as f:
        f.write("# mouse test result\n")

    # 屏幕提示
    msg = "Mouse test ready. Click anywhere, scroll wheel. Press 'q' to quit.\r\n"
    written = wintypes.DWORD(0)
    _kernel32.WriteConsoleW(h_out, msg, len(msg), ctypes.byref(written), None)

    buf = (INPUT_RECORD * 16)()
    while True:
        read = wintypes.DWORD(0)
        if not _kernel32.ReadConsoleInputW(h_in, buf, 16, ctypes.byref(read)):
            break
        for i in range(read.value):
            rec = buf[i]
            if rec.EventType == MOUSE_EVENT:
                me = rec.MouseEvent
                x = me.dwMousePosition.X
                y = me.dwMousePosition.Y
                btn = me.dwButtonState
                flags = me.dwEventFlags
                ctrl = me.dwControlKeyState
                line = "MOUSE {} {} {} {} {}\r\n".format(x, y, btn, flags, ctrl)
                # 写结果文件
                with open(result_file, "a", encoding="utf-8") as f:
                    f.write(line.strip() + "\n")
                # 屏幕显示
                _kernel32.WriteConsoleW(h_out, line, len(line), ctypes.byref(written), None)
            elif rec.EventType == KEY_EVENT:
                ke = rec.KeyEvent
                if ke.bKeyDown and ke.uChar in ("q", "Q"):
                    line = "QUIT\r\n"
                    _kernel32.WriteConsoleW(h_out, line, len(line), ctypes.byref(written), None)
                    with open(result_file, "a", encoding="utf-8") as f:
                        f.write("QUIT\n")
                    return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
