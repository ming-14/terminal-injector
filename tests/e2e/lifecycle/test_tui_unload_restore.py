"""特性: 全屏 TUI 卸载后 ConHost 恢复注入几何    类别: lifecycle

回归测试（2026-08-19 BUG-009 修复）:
  全屏 TUI（绝对坐标绘制、无行编辑 shell 语义）注入后，调小 WT 窗口再卸载，
  ConHost 曾被裁剪到会话尺寸并叠画会话 VT 帧（原窗口画面错乱/窗口缩小）。

  修复后：卸载时识别全屏 TUI（注入快照 isLineShell=false），跳过会话 VT
  重放，把 ConHost 缓冲(只放大)与窗口恢复到注入时几何，TUI 按轮询感知
  尺寸恢复并重绘完整画面。

断言:
  - 卸载后 ConHost buffer == 注入时 buffer（100x36）
  - 卸载后 ConHost 窗口 == 注入时窗口（[0,0]-[99,35]）
  - 卸载后可见画面 == 注入前画面（逐行一致，无叠画/错位/回绕）

链路: 真实 ConHost → 注入 → WT → WT resize → 卸载 → 原 ConHost 画面
验证方式: 目标程序自绘全屏帧（确定性字符矩阵）+ 独立进程 AttachConsole 读回
"""
import ctypes
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32gui
import win32con
from helpers import injector

NAME = "tui_unload_restore"

TARGET = r'''import ctypes
import ctypes.wintypes as wt
import os
import time

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CHAR_INFO(ctypes.Structure):
    _fields_ = [("Char", wt.WCHAR), ("Attributes", wt.WORD)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wt.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


GetStdHandle = kernel32.GetStdHandle
GetStdHandle.restype = wt.HANDLE
GetStdHandle.argtypes = [wt.DWORD]
GCSBI = kernel32.GetConsoleScreenBufferInfo
GCSBI.restype = wt.BOOL
GCSBI.argtypes = [wt.HANDLE, ctypes.POINTER(CSBI)]
WCOW = kernel32.WriteConsoleOutputW
WCOW.restype = wt.BOOL
WCOW.argtypes = [wt.HANDLE, ctypes.POINTER(CHAR_INFO), COORD, COORD,
                 ctypes.POINTER(SMALL_RECT)]

READY = os.path.join(os.environ["TI_TARGET_RESULT_DIR"], "ready.txt")


def win_size():
    info = CSBI()
    if not GCSBI(GetStdHandle(-11), ctypes.byref(info)):
        return (0, 0)
    return (info.srWindow.Right - info.srWindow.Left + 1,
            info.srWindow.Bottom - info.srWindow.Top + 1)


def paint(w, h):
    # 全屏确定性帧：第 y 行全为 chr('A'+y%26)，第 0 行首是 "TUI-FRAME" 标记
    cells = (CHAR_INFO * (w * h))()
    for y in range(h):
        ch = chr(ord("A") + (y % 26))
        for x in range(w):
            cells[y * w + x].Char = ch
            cells[y * w + x].Attributes = 0x07
    if h > 0:
        mark = "TUI-FRAME"
        for i, c in enumerate(mark):
            if i < w:
                cells[i].Char = c
    rect = SMALL_RECT(0, 0, w - 1, h - 1)
    if not WCOW(GetStdHandle(-11), cells, COORD(w, h), COORD(0, 0),
                ctypes.byref(rect)):
        print("paint failed", ctypes.get_last_error())


cur = win_size()
paint(*cur)
with open(READY, "w", encoding="utf-8") as f:
    f.write("READY=1\n")
# 循环：轮询窗口尺寸（劫持期间被 Hook 返回虚拟尺寸，卸载后返回真实尺寸），
# 变化即整帧重绘（与 winui/vim 等全屏 TUI 的 resize 反馈行为一致）
while True:
    time.sleep(0.2)
    s = win_size()
    if s != cur and s[0] > 0 and s[1] > 0:
        cur = s
        paint(*cur)
'''

DUMP_HELPER = r'''import argparse
import ctypes
import ctypes.wintypes as wt
import sys

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CHAR_INFO(ctypes.Structure):
    _fields_ = [("Char", wt.WCHAR), ("Attributes", wt.WORD)]


class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wt.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


ap = argparse.ArgumentParser()
ap.add_argument("--pid", type=int, required=True)
args = ap.parse_args()

kernel32.FreeConsole()
if not kernel32.AttachConsole(args.pid):
    print("ATTACH_FAIL gle=%d" % ctypes.get_last_error())
    sys.exit(1)

h = kernel32.CreateFileW("CONOUT$", 0xC0000000, 0x3, None, 3, 0, None)
info = CSBI()
if not kernel32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
    print("CSBI_FAIL gle=%d" % ctypes.get_last_error())
    sys.exit(1)

w = info.srWindow.Right - info.srWindow.Left + 1
print("BUFFER=%dx%d" % (info.dwSize.X, info.dwSize.Y))
print("WINDOW=%d,%d-%d,%d" % (info.srWindow.Left, info.srWindow.Top,
                              info.srWindow.Right, info.srWindow.Bottom))
print("FRAME_BEGIN")
for row in range(info.srWindow.Top, info.srWindow.Bottom + 1):
    buf = (CHAR_INFO * w)()
    r = SMALL_RECT(0, row, w - 1, row)
    n = wt.DWORD()
    if not kernel32.ReadConsoleOutputW(h, buf, COORD(w, 1), COORD(0, 0),
                                       ctypes.byref(r)):
        print("READ_FAIL gle=%d" % ctypes.get_last_error())
        sys.exit(1)
    print("".join(c.Char for c in buf).rstrip())
print("FRAME_END")
'''
_CACHE = {}


def _helper(name: str, code: str) -> str:
    if name not in _CACHE:
        d = tempfile.mkdtemp(prefix="ti_aux_")
        p = os.path.join(d, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(code)
        _CACHE[name] = p
    return _CACHE[name]


def start_target() -> int:
    """启动全屏 TUI 目标进程（独立控制台 100x36），返回 cmd pid。"""
    workdir = tempfile.mkdtemp(prefix="ti_tui_target_")
    tgt = _helper("tui_target.py", TARGET)
    env = dict(os.environ)
    env["TI_TARGET_RESULT_DIR"] = workdir
    proc = subprocess.Popen(
        ["cmd.exe", "/c", "mode con: cols=100 lines=36 >nul & python " + tgt],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        env=env,
    )
    ready = os.path.join(workdir, "ready.txt")
    deadline = time.time() + 15.0
    while time.time() < deadline and not os.path.exists(ready):
        time.sleep(0.2)
    if not os.path.exists(ready):
        print("  [FAIL] target not ready")
        return 0
    return proc.pid


def dump_con(pid: int) -> dict:
    """独立进程 AttachConsole 读回目标控制台画面与几何。"""
    helper = _helper("dump_helper.py", DUMP_HELPER)
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    out = subprocess.run(
        [sys.executable, helper, "--pid", str(pid)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
env=env, timeout=20,
    )
    if out.returncode != 0:
        return {"error": (out.stdout or out.stderr).strip()}
    res = {}
    lines = out.stdout.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("BUFFER="):
            res["buffer"] = tuple(int(v) for v in ln[7:].split("x"))
        elif ln.startswith("WINDOW="):
            res["window"] = tuple(int(v) for v in
                                  re.split(r"[,\-]", ln[7:]))
        elif ln == "FRAME_BEGIN":
            end = lines.index("FRAME_END", i)
            res["frame"] = lines[i + 1:end]
    return res


def run() -> int:
    pid = start_target()
    if not pid:
        return 1
    print("target pid={}".format(pid))
    mediator_proc = None
    try:
        pre = dump_con(pid)
        if "buffer" not in pre:
            print("  [FAIL] 注入前 dump 失败: {}".format(pre.get("error")))
            return 1
        print("pre-inject: buffer={} window={} rows={}".format(
            pre["buffer"], pre["window"], len(pre["frame"])))

        injector.clear_log(pid)
        mediator_proc = injector.start_wt_mediator(pid)
        if not injector.wait_for_handshake(pid, timeout=20.0):
            print("  [FAIL] 握手失败")
            return 1
        print("handshake OK")
        time.sleep(3.0)

        hwnd = injector._test_wt_hwnd
        if not hwnd:
            print("  [FAIL] 未获取 WT hwnd")
            return 1
        r = win32gui.GetWindowRect(hwnd)
        win32gui.SetWindowPos(hwnd, None, r[0], r[1],
                              int((r[2] - r[0]) * 0.6), r[3] - r[1], 0x0004)
        print("WT resized to 0.6x")
        time.sleep(4.0)

        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        deadline = time.time() + 10.0
        while time.time() < deadline and win32gui.IsWindow(hwnd):
            time.sleep(0.2)
        print("WT closed, waiting unload + TUI repaint...")
        time.sleep(4.0)

        post = dump_con(pid)
        if "buffer" not in post:
            print("  [FAIL] 卸载后 dump 失败: {}".format(post.get("error")))
            return 1
        print("post-unload: buffer={} window={} rows={}".format(
            post["buffer"], post["window"], len(post["frame"])))

        fails = 0

        # 断言 1：缓冲恢复到注入几何（100x36）
        if post["buffer"] != (100, 36):
            print("  [FAIL] buffer 未恢复注入几何: {} != (100,36)".format(post["buffer"]))
            fails += 1
        else:
            print("  [PASS] buffer 恢复为注入尺寸 100x36")

        # 断言 2：窗口恢复到注入几何
        if post["window"] != (0, 0, 99, 35):
            print("  [FAIL] window 未恢复注入几何: {} != (0,0,99,35)".format(post["window"]))
            fails += 1
        else:
            print("  [PASS] window 恢复为注入矩形 [0,0]-[99,35]")

        # 断言 3：画面与注入前逐行一致（无叠画/错位/回绕/残留）
        if post["frame"] != pre["frame"]:
            diff = [i for i, (a, b) in enumerate(zip(pre["frame"], post["frame"]))
                    if a != b][:5]
            print("  [FAIL] 卸载后画面与注入前不一致, 不同行索引前5个: {}".format(diff))
            for i in diff[:3]:
                print("      pre[{}]={!r}".format(i, pre["frame"][i]))
                print("      post[{}]={!r}".format(i, post["frame"][i]))
            if len(pre["frame"]) != len(post["frame"]):
                print("      row count pre={} post={}".format(
                    len(pre["frame"]), len(post["frame"])))
            fails += 1
        else:
            print("  [PASS] 画面与注入前逐行一致 ({}行)".format(len(post["frame"])))

        if fails == 0:
            print("SUMMARY: PASS (3 checks)")
            return 0
        print("SUMMARY: FAIL ({} checks)".format(fails))
        return 1
    finally:
        time.sleep(0.5)
        injector.cleanup(pid, mediator_proc)


if __name__ == "__main__":
    sys.exit(run())
