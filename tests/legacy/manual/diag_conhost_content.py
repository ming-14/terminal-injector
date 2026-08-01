"""
diag_conhost_content.py — 诊断脚本：读取 cmd 进程 ConHost 的实际屏幕内容

启动一个 cmd 进程，等待几秒后用 AttachConsole + ReadConsoleOutputW 读取 cmd 的
ConHost 屏幕内容，打印每行的字符和属性，验证版本横幅是否在 ConHost 中。

用法:
  python tests/manual/diag_conhost_content.py
"""
import os
import sys
import time
import ctypes
from ctypes import wintypes
import win32process
import win32api
import win32con

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class CHAR_INFO(ctypes.Structure):
    _fields_ = [
        ("UnicodeChar", wintypes.WCHAR),
        ("Attributes", wintypes.WORD),
    ]


class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", wintypes.SHORT),
        ("Top", wintypes.SHORT),
        ("Right", wintypes.SHORT),
        ("Bottom", wintypes.SHORT),
    ]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD),
    ]


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# BOOL ReadConsoleOutputW(HANDLE hConsoleOutput, LPCHAR_INFO lpBuffer,
#   COORD dwBufferSize, COORD dwBufferCoord, PSMALL_RECT lpReadRegion)
kernel32.ReadConsoleOutputW.restype = wintypes.BOOL
kernel32.ReadConsoleOutputW.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(CHAR_INFO),
    COORD, COORD, ctypes.POINTER(SMALL_RECT)
]

kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
kernel32.GetConsoleScreenBufferInfo.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)
]

kernel32.AttachConsole.restype = wintypes.BOOL
kernel32.AttachConsole.argtypes = [wintypes.DWORD]

kernel32.FreeConsole.restype = wintypes.BOOL
kernel32.FreeConsole.argtypes = []

kernel32.GetStdHandle.restype = wintypes.HANDLE
kernel32.GetStdHandle.argtypes = [wintypes.DWORD]

ATTACH_PARENT_PROCESS = 0xFFFFFFFF
STD_OUTPUT_HANDLE = wintypes.DWORD(0xFFFFFFF5)


def start_cmd():
    si = win32process.STARTUPINFO()
    si.dwFlags = win32con.STARTF_USESHOWWINDOW
    si.wShowWindow = win32con.SW_SHOW
    # 纯 cmd.exe 启动（会输出版本横幅 + prompt）
    # 工作目录通过 CreateProcess 的 lpCurrentDirectory 参数指定
    cmd_line = 'cmd.exe'
    handle, thread_handle, pid, tid = win32process.CreateProcess(
        None, cmd_line, None, None, False,
        win32con.CREATE_NEW_CONSOLE,
        None, PROJECT_ROOT, si)
    win32api.CloseHandle(handle)
    win32api.CloseHandle(thread_handle)
    return pid


def read_conhost_screen(pid):
    """Attach 到 pid 的 console，读取屏幕内容，返回 (csbi, cells)"""
    # 先从当前进程 free console
    kernel32.FreeConsole()
    time.sleep(0.1)
    # attach 到目标进程的 console
    if not kernel32.AttachConsole(wintypes.DWORD(pid)):
        err = ctypes.get_last_error()
        print(f"AttachConsole failed: err={err}")
        return None, None

    try:
        hOut = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        csbi = CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(hOut, ctypes.byref(csbi)):
            err = ctypes.get_last_error()
            print(f"GetConsoleScreenBufferInfo failed: err={err}")
            return None, None

        win = csbi.srWindow
        width = win.Right - win.Left + 1
        height = win.Bottom - win.Top + 1
        print(f"ConHost srWindow: ({win.Left},{win.Top})-({win.Right},{win.Bottom}) "
              f"size={width}x{height}")
        print(f"ConHost cursor: ({csbi.dwCursorPosition.X},{csbi.dwCursorPosition.Y})")

        # 读取屏幕内容
        cells = (CHAR_INFO * (width * height))()
        bufSize = COORD(width, height)
        bufCoord = COORD(0, 0)
        readRegion = SMALL_RECT(win.Left, win.Top, win.Right, win.Bottom)
        if not kernel32.ReadConsoleOutputW(
                hOut, cells, bufSize, bufCoord, ctypes.byref(readRegion)):
            err = ctypes.get_last_error()
            print(f"ReadConsoleOutputW failed: err={err}")
            return csbi, None
        return csbi, cells
    finally:
        kernel32.FreeConsole()


def print_screen(csbi, cells):
    if cells is None:
        return
    win = csbi.srWindow
    width = win.Right - win.Left + 1
    height = win.Bottom - win.Top + 1

    print()
    print("=" * 80)
    print(f"ConHost 屏幕内容（{width}x{height}）：")
    print("=" * 80)
    for r in range(height):
        line_chars = []
        line_attrs = []
        for c in range(width):
            ci = cells[r * width + c]
            ch = ci.UnicodeChar
            if ch == '\x00':
                ch = ' '
            line_chars.append(ch)
            line_attrs.append(ci.Attributes)
        line = ''.join(line_chars).rstrip()
        if line:
            # 显示前 10 个 cell 的属性
            attr_str = ' '.join(f'{a:04x}' for a in line_attrs[:10])
            print(f"{r:2d}| {line}")
            print(f"    attrs[0:10]: {attr_str}")
    print("=" * 80)


def main():
    print("启动 cmd 进程...")
    pid = start_cmd()
    print(f"cmd pid={pid}, 等待 5 秒输出横幅+prompt...")
    time.sleep(5.0)

    print(f"读取 pid={pid} 的 ConHost 内容...")
    csbi, cells = read_conhost_screen(pid)

    if csbi:
        print_screen(csbi, cells)

    # 清理
    try:
        import psutil
        p = psutil.Process(pid)
        p.kill()
        p.wait(timeout=3)
        print(f"cmd killed, pid={pid}")
    except Exception as e:
        print(f"cleanup failed: {e}")


if __name__ == "__main__":
    main()
