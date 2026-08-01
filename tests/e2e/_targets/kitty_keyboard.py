# -*- coding: utf-8 -*-
"""目标脚本（自动生成，勿手改）。"""
import os
import sys
import time
import ctypes
from ctypes import wintypes

# ---- 结果文件 ----
RESULT_FILE = sys.argv[1] if len(sys.argv) > 1 else "target_result.txt"

def rec(key, value="1"):
    with open(RESULT_FILE, "a", encoding="utf-8") as f:
        f.write("{}={}\n".format(key, value))

def check(name, cond, detail=""):
    if cond:
        rec(name, "PASS")
    else:
        rec(name, "FAIL:" + str(detail))

def done():
    rec("DONE", "1")

# ---- 控制台常量 ----
STD_INPUT_HANDLE = ctypes.c_ulong(-10).value
STD_OUTPUT_HANDLE = ctypes.c_ulong(-11).value
STD_ERROR_HANDLE = ctypes.c_ulong(-12).value

ENABLE_PROCESSED_INPUT = 0x0001
ENABLE_LINE_INPUT = 0x0002
ENABLE_ECHO_INPUT = 0x0004
ENABLE_WINDOW_INPUT = 0x0008
ENABLE_MOUSE_INPUT = 0x0010
ENABLE_INSERT_MODE = 0x0020
ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
ENABLE_AUTO_POSITION = 0x0100
ENABLE_VIRTUAL_TERMINAL_INPUT = 0x0200
ENABLE_PROCESSED_OUTPUT = 0x0001
ENABLE_WRAP_AT_EOL_OUTPUT = 0x0002
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
ENABLE_DISABLE_NEWLINE_AUTO_RETURN = 0x0008
ENABLE_LVB_GRID_WORLDWIDE = 0x0010

KEY_EVENT = 0x0001
MOUSE_EVENT = 0x0002
WINDOW_BUFFER_SIZE_EVENT = 0x0004
MENU_EVENT = 0x0008
FOCUS_EVENT = 0x0010

FROM_LEFT_1ST_BUTTON_PRESSED = 0x0001
RIGHTMOST_BUTTON_PRESSED = 0x0002
FROM_LEFT_2ND_BUTTON_PRESSED = 0x0004
FROM_LEFT_3RD_BUTTON_PRESSED = 0x0008
FROM_LEFT_4TH_BUTTON_PRESSED = 0x0010
MOUSE_WHEELED_BIT = 0x0003

MOUSE_MOVED = 0x0001
MOUSE_DOUBLE_CLICK = 0x0002
MOUSE_WHEELED = 0x0004
MOUSE_HWHEELED = 0x0008

SHIFT_PRESSED = 0x0010
LEFT_ALT_PRESSED = 0x0002
LEFT_CTRL_PRESSED = 0x0008
RIGHT_ALT_PRESSED = 0x0001
RIGHT_CTRL_PRESSED = 0x0004
CAPSLOCK_ON = 0x0080
NUMLOCK_ON = 0x0020
SCROLLLOCK_ON = 0x0040

FOREGROUND_BLUE = 0x0001
FOREGROUND_GREEN = 0x0002
FOREGROUND_RED = 0x0004
FOREGROUND_INTENSITY = 0x0008
BACKGROUND_BLUE = 0x0010
BACKGROUND_GREEN = 0x0020
BACKGROUND_RED = 0x0040
BACKGROUND_INTENSITY = 0x0080

# ---- 结构体 ----
class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]

class CHAR_INFO(ctypes.Structure):
    _fields_ = [("Char", wintypes.WCHAR), ("Attributes", wintypes.WORD)]

class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]

class CONSOLE_CURSOR_INFO(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("bVisible", wintypes.BOOL)]

class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]

class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwMousePosition", COORD), ("dwButtonState", wintypes.DWORD),
                ("dwControlKeyState", wintypes.DWORD), ("dwEventFlags", wintypes.DWORD)]

class WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
    _fields_ = [("dwSize", COORD)]

class MENU_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwCommandId", wintypes.UINT)]

class FOCUS_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bSetFocus", wintypes.BOOL)]

class _INPUT_RECORD_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("MouseEvent", MOUSE_EVENT_RECORD),
                ("WindowBufferSizeEvent", WINDOW_BUFFER_SIZE_RECORD),
                ("MenuEvent", MENU_EVENT_RECORD), ("FocusEvent", FOCUS_EVENT_RECORD)]

class INPUT_RECORD(ctypes.Structure):
    _anonymous_ = ("_event",)
    _fields_ = [("EventType", wintypes.WORD), ("_event", _INPUT_RECORD_UNION)]

# ---- kernel32 绑定 ----
_k = ctypes.windll.kernel32
_k.GetStdHandle.argtypes = [wintypes.DWORD]
_k.GetStdHandle.restype = wintypes.HANDLE
_k.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k.GetConsoleMode.restype = wintypes.BOOL
_k.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k.SetConsoleMode.restype = wintypes.BOOL
_k.WriteConsoleW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k.WriteConsoleW.restype = wintypes.BOOL
_k.WriteConsoleA.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                             ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k.WriteConsoleA.restype = wintypes.BOOL
_k.ReadConsoleW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k.ReadConsoleW.restype = wintypes.BOOL
_k.ReadConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD),
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_k.ReadConsoleInputW.restype = wintypes.BOOL
_k.PeekConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD),
                                 wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
_k.PeekConsoleInputW.restype = wintypes.BOOL
_k.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE,
                                          ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
_k.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
_k.SetConsoleCursorPosition.argtypes = [wintypes.HANDLE, COORD]
_k.SetConsoleCursorPosition.restype = wintypes.BOOL
_k.GetConsoleCursorInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CONSOLE_CURSOR_INFO)]
_k.GetConsoleCursorInfo.restype = wintypes.BOOL
_k.SetConsoleCursorInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CONSOLE_CURSOR_INFO)]
_k.SetConsoleCursorInfo.restype = wintypes.BOOL
_k.SetConsoleTextAttribute.argtypes = [wintypes.HANDLE, wintypes.WORD]
_k.SetConsoleTextAttribute.restype = wintypes.BOOL
_k.GetConsoleTitleW.argtypes = [ctypes.c_wchar_p, wintypes.DWORD]
_k.GetConsoleTitleW.restype = wintypes.DWORD
_k.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
_k.SetConsoleTitleW.restype = wintypes.BOOL
_k.FillConsoleOutputCharacterW.argtypes = [wintypes.HANDLE, wintypes.WCHAR,
                                           wintypes.DWORD, COORD,
                                           ctypes.POINTER(wintypes.DWORD)]
_k.FillConsoleOutputCharacterW.restype = wintypes.BOOL
_k.FillConsoleOutputAttribute.argtypes = [wintypes.HANDLE, wintypes.WORD,
                                          wintypes.DWORD, COORD,
                                          ctypes.POINTER(wintypes.DWORD)]
_k.FillConsoleOutputAttribute.restype = wintypes.BOOL
_k.WriteConsoleOutputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(CHAR_INFO),
                                   COORD, COORD, ctypes.POINTER(SMALL_RECT)]
_k.WriteConsoleOutputW.restype = wintypes.BOOL
_k.WriteConsoleOutputAttribute.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.WORD),
                                           wintypes.DWORD, COORD,
                                           ctypes.POINTER(wintypes.DWORD)]
_k.WriteConsoleOutputAttribute.restype = wintypes.BOOL
_k.ScrollConsoleScreenBufferW.argtypes = [wintypes.HANDLE, ctypes.POINTER(SMALL_RECT),
                                          ctypes.POINTER(SMALL_RECT), COORD,
                                          ctypes.POINTER(CHAR_INFO)]
_k.ScrollConsoleScreenBufferW.restype = wintypes.BOOL
_k.CreateConsoleScreenBuffer.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                         ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
_k.CreateConsoleScreenBuffer.restype = wintypes.HANDLE
_k.SetConsoleActiveScreenBuffer.argtypes = [wintypes.HANDLE]
_k.SetConsoleActiveScreenBuffer.restype = wintypes.BOOL
_k.SetConsoleScreenBufferSize.argtypes = [wintypes.HANDLE, COORD]
_k.SetConsoleScreenBufferSize.restype = wintypes.BOOL
_k.SetConsoleWindowInfo.argtypes = [wintypes.HANDLE, wintypes.BOOL,
                                    ctypes.POINTER(SMALL_RECT)]
_k.SetConsoleWindowInfo.restype = wintypes.BOOL
_k.GetConsoleCP.argtypes = []
_k.GetConsoleCP.restype = wintypes.UINT
_k.SetConsoleCP.argtypes = [wintypes.UINT]
_k.SetConsoleCP.restype = wintypes.BOOL
_k.GetConsoleOutputCP.argtypes = []
_k.GetConsoleOutputCP.restype = wintypes.UINT
_k.SetConsoleOutputCP.argtypes = [wintypes.UINT]
_k.SetConsoleOutputCP.restype = wintypes.BOOL
_k.FlushConsoleInputBuffer.argtypes = [wintypes.HANDLE]
_k.FlushConsoleInputBuffer.restype = wintypes.BOOL
_k.GetNumberOfConsoleInputEvents.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k.GetNumberOfConsoleInputEvents.restype = wintypes.BOOL
_k.GetLargestConsoleWindowSize.argtypes = [wintypes.HANDLE]
_k.GetLargestConsoleWindowSize.restype = COORD
_k.GetConsoleWindow.argtypes = []
_k.GetConsoleWindow.restype = wintypes.HWND
_k.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
_k.GetConsoleProcessList.restype = wintypes.DWORD
_k.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                        ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k.ReadFile.restype = wintypes.BOOL
_k.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                         ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
_k.WriteFile.restype = wintypes.BOOL
_k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_k.WaitForSingleObject.restype = wintypes.DWORD
_k.GetConsoleInputWaitHandle.argtypes = []
_k.GetConsoleInputWaitHandle.restype = wintypes.HANDLE

WAIT_TIMEOUT = 0x00000102
INFINITE = 0xFFFFFFFF
GENERIC_READ_WRITE = 0x40000000
FILE_SHARE_READ_WRITE = 0x00000003
OPEN_EXISTING = 3
CONSOLE_TEXTMODE_BUFFER = 1

# ---- 常用辅助函数 ----
def get_std_in():
    return _k.GetStdHandle(STD_INPUT_HANDLE)

def get_input_wait_handle():
    """DLL Hook GetConsoleInputWaitHandle 返回 InputQueue 事件句柄。

    等待它才能感知 DLL 队列输入；直接等待 stdin 句柄会被真实 ConHost
    残留事件常置位，导致 ReadConsoleInputW 空转（详见 F11 全屏调试）。
    """
    return _k.GetConsoleInputWaitHandle()

def get_std_out():
    return _k.GetStdHandle(STD_OUTPUT_HANDLE)

def get_mode(h):
    m = wintypes.DWORD(0)
    if not _k.GetConsoleMode(h, ctypes.byref(m)):
        return -1
    return m.value

def set_mode(h, mode):
    return _k.SetConsoleMode(h, mode)

def write_str(h, s):
    """WriteConsoleW 写入；nNumberOfCharsToWrite 按 wchar 单位数（代理对=2）。"""
    n = wintypes.DWORD(0)
    wlen = len(s.encode("utf-16-le")) // 2
    ok = _k.WriteConsoleW(h, s, wlen, ctypes.byref(n), None)
    return ok, n.value

def write_bytes(h, data):
    """WriteFile 写原始字节（VT 直通/老式路径测试用）。"""
    buf = ctypes.create_string_buffer(data, len(data))
    n = wintypes.DWORD(0)
    ok = _k.WriteFile(h, buf, len(data), ctypes.byref(n), None)
    return ok, n.value

def get_csbi(h):
    info = CONSOLE_SCREEN_BUFFER_INFO()
    if not _k.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
        return None
    return info

def cursor_pos(h=None):
    info = get_csbi(h if h is not None else get_std_out())
    if info is None:
        return (-1, -1)
    return (info.dwCursorPosition.X, info.dwCursorPosition.Y)

def read_input_records(h, count, peek=False):
    """读取（或 Peek）count 个输入记录，返回 INPUT_RECORD 列表。"""
    buf = (INPUT_RECORD * count)()
    n = wintypes.DWORD(0)
    fn = _k.PeekConsoleInputW if peek else _k.ReadConsoleInputW
    if not fn(h, buf, count, ctypes.byref(n)):
        return []
    return list(buf[: n.value])

def wait_input(h, timeout_ms=5000):
    """等待输入句柄有数据，返回 True/False（超时）。"""
    r = _k.WaitForSingleObject(h, timeout_ms)
    return r != WAIT_TIMEOUT


rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_VIRTUAL_TERMINAL_INPUT)
h_out = get_std_out()
ok, _ = write_bytes(h_out, b"\x1b[>1u")
rec("SENT", str(int(ok)))
import os as _os
import threading
res = []
t = threading.Thread(target=lambda: res.append(_os.read(0, 256)))
t.daemon = True
t.start()
t.join(4.0)
b = res[0] if res else b""
rec("GOT", b.hex() if b else "TIMEOUT")
done()
