# -*- coding: utf-8 -*-
"""读取目标控制台几何（供 e2e 测试子进程调用）。

用法: python dump_geom.py <pid>
输出: cols rows bufX bufY （stdout 一行；失败输出 FAIL:<错误码>）

独立进程运行的原因：FreeConsole->AttachConsole 在"测试进程自身继承控制台"
的环境（run_all subprocess 管道模式）下会失败（gle=6）；独立进程 + 显式
FreeConsole 与 dump_console.py 相同，实测可靠。
"""
import ctypes
import ctypes.wintypes as wt
import sys

k = ctypes.WinDLL("kernel32", use_last_error=True)


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wt.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


def main() -> int:
    pid = int(sys.argv[1])
    k.FreeConsole()
    if not k.AttachConsole(pid):
        print("FAIL:AttachConsole gle={}".format(ctypes.get_last_error()))
        return 1
    h = k.CreateFileW("CONOUT$", 0xC0000000, 0x3, None, 3, 0, None)
    if h in (None, wt.HANDLE(-1).value):
        print("FAIL:CONOUT$ gle={}".format(ctypes.get_last_error()))
        return 1
    csbi = CSBI()
    if not k.GetConsoleScreenBufferInfo(h, ctypes.byref(csbi)):
        print("FAIL:GCSBI gle={}".format(ctypes.get_last_error()))
        return 1
    k.CloseHandle(h)
    cols = csbi.srWindow.Right - csbi.srWindow.Left + 1
    rows = csbi.srWindow.Bottom - csbi.srWindow.Top + 1
    print("{} {} {} {}".format(cols, rows, csbi.dwSize.X, csbi.dwSize.Y))
    return 0


if __name__ == "__main__":
    sys.exit(main())