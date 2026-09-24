# -*- coding: utf-8 -*-
"""console_buffer_shape_probe.py —— 实测「经典 ConHost / ConPTY / WT」× 「主/备用屏」下
控制台各项属性的取值，用来给 LazyInit 的 shell/TUI 判据找可区分的信号。

背景（用户报告）
----------------
injected.dll 的 LazyInit 用 `isLineShell = !bufMatchesWin` 区分行编辑 shell 与全屏 TUI，
其中 `bufMatchesWin = (dwSize == 窗口尺寸)`，注释里的依据是"全屏 TUI 用 alt buffer，
缓冲==窗口，无滚动历史"。

用户把**跑在正常 WT 里的 pwsh**注入到另一个 WT → 画面全空、光标停在左下角。
DLL 日志：`lineShell=0 (echoInput=1 bufMatchesWin=1 buf=156x42 win=156x42)`
—— 被当成全屏 TUI：发 ?1049h 切备用屏 → 尺寸不匹配 → 跳过屏幕重放 → 光标停在 (0,41)。

原因：ConPTY 托管的控制台**根本没有滚动历史**（缓冲恒等于窗口），
"buf==win ⇒ alt buffer"这条代理失效。而经典 ConHost 的 alt buffer 与
ConPTY 的主 buffer 恰好都满足 buf==win —— 必须另找信号。

本探针测量 4~5 种形态下的候选属性，输出对照表：
  classic-main / classic-alt / conpty-main / conpty-alt / wt-main
候选信号：buf vs win、dwMaximumWindowSize、GetLargestConsoleWindowSize、
          GetConsoleWindow 的可见性/类名/宿主进程、输入模式、
          SetConsoleScreenBufferSize 能否加高（即"是否允许滚动历史"）、WT_SESSION。

    python console_buffer_shape_probe.py
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, "_bufshape")
CHILD = os.path.join(TMP, "probe_child.py")

_PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.normpath(
    os.path.join(HERE, "..", "..", "reference"))
if os.path.isdir(_PWTERM_DIR) and _PWTERM_DIR not in sys.path:
    sys.path.insert(0, _PWTERM_DIR)

CHILD_SRC = r'''# -*- coding: utf-8 -*-
import ctypes, os, sys, time
from ctypes import wintypes

k = ctypes.windll.kernel32
u = ctypes.windll.user32

class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]

class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]

k.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CSBI)]
k.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
k.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k.GetConsoleMode.restype = wintypes.BOOL
k.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k.SetConsoleMode.restype = wintypes.BOOL
k.GetLargestConsoleWindowSize.argtypes = [wintypes.HANDLE]
k.GetLargestConsoleWindowSize.restype = COORD
k.SetConsoleScreenBufferSize.argtypes = [wintypes.HANDLE, COORD]
k.SetConsoleScreenBufferSize.restype = wintypes.BOOL

# HWND 相关必须显式声明 argtypes：64 位下不声明会把 HWND 当 C int 截断，
# 拿到的窗口属性全是假的（本探针初版就在这里出过一次误判）。
k.GetConsoleWindow.argtypes = []
k.GetConsoleWindow.restype = wintypes.HWND
u.IsWindowVisible.argtypes = [wintypes.HWND]
u.IsWindowVisible.restype = wintypes.BOOL
u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.GetClassNameW.restype = ctypes.c_int
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
u.GetWindowThreadProcessId.restype = wintypes.DWORD
u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.GetWindowTextW.restype = ctypes.c_int

out_path = sys.argv[1]
enter_alt = "--alt" in sys.argv

h_out = k.GetStdHandle(-11)
h_in = k.GetStdHandle(-10)

# 先写几行内容，贴近"shell 已经打印过 banner/prompt"
for i in range(3):
    print("filler line %d" % i, flush=True)
time.sleep(0.3)

alt_ok = 0
if enter_alt:
    # 打开 VT 处理，然后发 ?1049h 切备用屏（经典 ConHost 与 ConPTY/WT 都认）
    m = wintypes.DWORD(0)
    k.GetConsoleMode(h_out, ctypes.byref(m))
    k.SetConsoleMode(h_out, m.value | 0x0004)
    os.write(1, b"\x1b[?1049h")
    time.sleep(0.4)

info = CSBI()
ok = k.GetConsoleScreenBufferInfo(h_out, ctypes.byref(info))
mode = wintypes.DWORD(0)
mode_ok = k.GetConsoleMode(h_in, ctypes.byref(mode))
largest = k.GetLargestConsoleWindowSize(h_out)

# 关键候选信号：这台控制台到底能不能"缓冲区高于窗口"（= 拥有滚动历史）。
# ConPTY 托管的控制台是视口模型，理论上不该有滚动历史 → 请求加高应被钳制/拒绝。
# 注意：必须在**还原之前**读回尺寸，否则测不到是否真的变了。
grow_ok, grow_err, grew_to = 0, 0, "?"
if ok:
    want_y = info.dwSize.Y + 500
    r = k.SetConsoleScreenBufferSize(h_out, COORD(info.dwSize.X, want_y))
    grow_err = ctypes.get_last_error() if not r else 0
    grow_ok = 1 if r else 0
    after = CSBI()
    k.GetConsoleScreenBufferInfo(h_out, ctypes.byref(after))
    grew_to = "%dx%d(want %dx%d)" % (after.dwSize.X, after.dwSize.Y,
                                    info.dwSize.X, want_y)
    if r:
        k.SetConsoleScreenBufferSize(h_out, info.dwSize)   # 还原

hwnd = k.GetConsoleWindow()
hwnd_visible = int(u.IsWindowVisible(hwnd)) if hwnd else -1
cls = ctypes.create_unicode_buffer(256)
title = ctypes.create_unicode_buffer(256)
owner = ""
if hwnd:
    u.GetClassNameW(hwnd, cls, 256)
    u.GetWindowTextW(hwnd, title, 256)
    pid = wintypes.DWORD(0)
    u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        import psutil
        owner = psutil.Process(pid.value).name()
    except Exception:
        owner = "pid=%d" % pid.value

res = [
    ("csbi_ok", int(ok)),
    ("buf", "%dx%d" % (info.dwSize.X, info.dwSize.Y)),
    ("win", "%dx%d" % (info.srWindow.Right - info.srWindow.Left + 1,
                       info.srWindow.Bottom - info.srWindow.Top + 1)),
    ("max_win", "%dx%d" % (info.dwMaximumWindowSize.X, info.dwMaximumWindowSize.Y)),
    ("largest", "%dx%d" % (largest.X, largest.Y)),
    ("grow_buf_ok", int(grow_ok)),
    ("grow_buf_err", int(grow_err)),
    ("grow_buf_to", grew_to),
    ("input_mode", "0x%lx" % mode.value),
    ("console_hwnd", "0x%x" % hwnd if hwnd else "0"),
    ("hwnd_visible", int(hwnd_visible)),
    ("hwnd_class", cls.value),
    ("hwnd_title", title.value[:40]),
    ("hwnd_owner", owner),
    ("WT_SESSION", os.environ.get("WT_SESSION", "")),
]
with open(out_path, "w", encoding="utf-8") as f:
    for kk, vv in res:
        f.write("%s=%s\n" % (kk, vv))
'''


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return dict(l.split("=", 1) for l in f.read().splitlines() if "=" in l)
    except OSError:
        return {}


def _wait_file(path, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            time.sleep(0.25)
            return True
        time.sleep(0.2)
    return False


def run_child(out_name, alt=False, host="conhost"):
    out = os.path.join(TMP, out_name)
    _rm(out)
    args = [sys.executable, CHILD, out] + (["--alt"] if alt else [])

    if host == "conhost":
        p = subprocess.Popen(args, cwd=TMP,
                             creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10))
        ok = _wait_file(out)
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()
        return ok, _read(out)

    if host == "conpty":
        try:
            import pywezterm
        except ImportError as e:
            return None, {"error": "无法 import pywezterm: {}".format(e)}
        pty = pywezterm.Pty(100, 30)
        try:
            pty.spawn(args)
        except Exception as e:  # noqa: BLE001
            pty.close()
            return None, {"error": str(e)}
        ok = _wait_file(out)
        time.sleep(0.4)
        try:
            pty.close()
        except Exception:
            pass
        return ok, _read(out)

    # host == "wt"
    wt = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                      "Microsoft", "WindowsApps", "wt.exe")
    if not os.path.exists(wt):
        return None, {"error": "wt.exe 不存在"}
    win = "ti_bufshape_{}_{}".format(out_name, int(time.time() * 1000))
    subprocess.Popen([wt, "-w", win, "--"] + args, cwd=TMP)
    ok = _wait_file(out, timeout=25.0)
    time.sleep(0.5)
    try:
        import win32gui
        import win32con
        win32gui.EnumWindows(_close_probe_window, None)
    except Exception:
        pass
    return ok, _read(out)


def _close_probe_window(hwnd, _):
    try:
        import ctypes
        import win32gui
        import win32con
        buf = ctypes.create_unicode_buffer(512)
        win32gui.GetWindowTextW(hwnd, buf, 512)
        if "probe_child" in buf.value and win32gui.IsWindowVisible(hwnd):
            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    except Exception:
        pass


def show(name, d):
    if not d:
        print("  {:<14} 未能取到数据".format(name))
        return
    if "error" in d:
        print("  {:<14} 跳过: {}".format(name, d["error"]))
        return
    same = "1(=TUI)" if d["buf"] == d["win"] else "0(=shell)"
    print("  {:<14} buf={:<9} win={:<9} max_win={:<8} largest={:<8} "
          "grow允许={}({}->{}) mode={:<7} hwnd可见={:<3} 宿主={:<14} "
          "类={:<18} 标题={:<16} bufMatchesWin={}".format(
              name, d["buf"], d["win"], d["max_win"], d["largest"],
              d.get("grow_buf_ok", "?"), d.get("grow_buf_err", "?"),
              d.get("grow_buf_to", "?"), d.get("input_mode", "?"),
              d.get("hwnd_visible", "?"), d.get("hwnd_owner", "?") or "-",
              d.get("hwnd_class", "?") or "-", d.get("hwnd_title", "?") or "-", same))


def main():
    os.makedirs(TMP, exist_ok=True)
    with open(CHILD, "w", encoding="utf-8") as f:
        f.write(CHILD_SRC)

    print("=" * 130)
    print("控制台属性对照：经典 ConHost / ConPTY / WT  ×  主屏 / 备用屏")
    print("=" * 130)

    cases = [
        ("classic-main", dict(out_name="classic_main.txt", alt=False, host="conhost")),
        ("classic-alt", dict(out_name="classic_alt.txt", alt=True, host="conhost")),
        ("conpty-main", dict(out_name="conpty_main.txt", alt=False, host="conpty")),
        ("conpty-alt", dict(out_name="conpty_alt.txt", alt=True, host="conpty")),
        ("wt-main", dict(out_name="wt_main.txt", alt=False, host="wt")),
        ("wt-alt", dict(out_name="wt_alt.txt", alt=True, host="wt")),
    ]
    data = {}
    for name, kw in cases:
        ok, d = run_child(**kw)
        data[name] = d if ok else {}
        show(name, data[name])

    print("\n" + "=" * 130)
    print("推导：哪个属性能把「经典 ConHost 主屏」与「ConPTY 主屏」分开，")
    print("      同时把「ConPTY 主屏(普通 shell)」与「ConPTY 备用屏(全屏 TUI)」分开")
    print("=" * 130)
    def get(n, key):
        return data.get(n, {}).get(key, "?")
    print("  buf==win: "
          + ", ".join("{}({})".format(n, "1" if get(n, "buf") == get(n, "win") else "0")
                      for n in ("classic-main", "classic-alt", "conpty-main",
                                "conpty-alt", "wt-main", "wt-alt")))
    print("  grow允许: "
          + ", ".join("{}({})".format(n, get(n, "grow_buf_ok"))
                      for n in ("classic-main", "classic-alt", "conpty-main",
                                "conpty-alt", "wt-main", "wt-alt")))
    print("  hwnd可见: "
          + ", ".join("{}({})".format(n, get(n, "hwnd_visible"))
                      for n in ("classic-main", "classic-alt", "conpty-main",
                                "conpty-alt", "wt-main", "wt-alt")))
    print("  hwnd宿主: "
          + ", ".join("{}({})".format(n, get(n, "hwnd_owner"))
                      for n in ("classic-main", "classic-alt", "conpty-main",
                                "conpty-alt", "wt-main", "wt-alt")))
    print("  input_mode: "
          + ", ".join("{}({})".format(n, get(n, "input_mode"))
                      for n in ("classic-main", "classic-alt", "conpty-main",
                                "conpty-alt", "wt-main", "wt-alt")))
    print("=" * 130)
    return 0


if __name__ == "__main__":
    sys.exit(main())
