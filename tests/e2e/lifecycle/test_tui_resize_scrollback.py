"""特性: 注入后 resize WT 窗口不产生 scrollback    类别: lifecycle

回归测试（2026-08-18 Bug B 修复，方案 A = LazyInit 补发 ?1049h）:
  全屏 TUI（vim）在私有 conhost 启动，其 `\x1b[?1049h`（切 alt buffer）
  发生在注入之前未达 WT → WT 侧 vim 画面停留在【主 buffer】；WT resize
  触发的 vim CLEAR 重绘（ED 2J + 全屏，经 DLL 直通）在主 buffer 语义下
  把视口整屏推入 scrollback → 滚动条 + 双帧（UIA 实测 58 行）。
  修复后：注入时向 WT 补发 ?1049h（alt buffer），ED 2J 只清空不推
  scrollback → resize 后 UIA 29 行（与原生一致）。

断言:
  - 注入后 UIA 读 TermControl 基线行数（非空白）
  - SetWindowPos 缩窄 WT 窗口后，非空白行数不得明显增长
    （修复前 +28 行 scrollback；修复后 ±1）

依赖: 真实 vim（TI_VIM_EXE 或常见安装路径；找不到 → UNSUPPORTED）、
      UIA 读 WT TermControl（需窗口可见）、pywin32。
"""
import ctypes
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32gui
import uiautomation as auto
from helpers import injector

NAME = "tui_resize_scrollback"

# vim 查找顺序：TI_VIM_EXE 环境变量 → 常见安装路径 → PATH
_VIM_CANDIDATES = [
    os.environ.get("TI_VIM_EXE", ""),
    r"C:\Program Files\Vim\vim92\vim.exe",
    r"C:\Program Files (x86)\Vim\vim92\vim.exe",
    r"C:\Vim\vim92\vim.exe",
]


def find_vim() -> str:
    for c in _VIM_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    import shutil
    return shutil.which("vim") or ""


def uia_read_wt(hwnd):
    """UIA 读 WT TermControl 全部文本（scrollback + 视口）。"""
    win = auto.ControlFromHandle(hwnd)
    stack = [win]
    term = None
    while stack:
        cur = stack.pop()
        try:
            children = cur.GetChildren()
        except Exception:
            children = []
        for ch in children:
            try:
                clsn = ch.ClassName or ""
            except Exception:
                clsn = ""
            if "TermControl" in clsn:
                term = ch
                stack.clear()
                break
            stack.append(ch)
    if term is None:
        return None
    tp = term.GetTextPattern()
    if tp is None:
        return None
    return tp.DocumentRange.GetText(-1)


def count_nonblank(text):
    return len([l for l in text.splitlines() if l.strip()])


def run() -> int:
    vim = find_vim()
    if not vim:
        print("SUMMARY: UNSUPPORTED (vim not found, set TI_VIM_EXE)")
        return 0

    proc = subprocess.Popen([vim, "-u", "NONE", "--noplugin"],
                            creationflags=subprocess.CREATE_NEW_CONSOLE)
    pid = proc.pid
    print("vim pid={} exe={}".format(pid, vim))
    time.sleep(3.0)

    mediator_proc = None
    try:
        injector.clear_log(pid)
        mediator_proc = injector.start_wt_mediator(pid)
        if not injector.wait_for_handshake(pid, timeout=20.0):
            print("  [FAIL] 握手失败")
            return 1
        print("inject handshake = True")
        time.sleep(6.0)

        hwnd = injector._test_wt_hwnd
        if not hwnd:
            print("  [FAIL] 未获取 WT hwnd")
            return 1

        t0 = uia_read_wt(hwnd)
        if t0 is None:
            print("  [FAIL] 基线 UIA 读取失败")
            return 1
        base_total = len(t0.splitlines())
        base_nb = count_nonblank(t0)
        print("baseline: total={} nonblank={}".format(base_total, base_nb))

        r2 = win32gui.GetWindowRect(hwnd)
        win32gui.SetWindowPos(hwnd, None, r2[0], r2[1],
                              int((r2[2] - r2[0]) * 0.75), r2[3] - r2[1], 0x0004)
        print("WT resized: {}x{} -> {}x{}".format(
            r2[2] - r2[0], r2[3] - r2[1],
            int((r2[2] - r2[0]) * 0.75), r2[3] - r2[1]))
        time.sleep(6.0)

        t1 = uia_read_wt(hwnd)
        if t1 is None:
            print("  [FAIL] resize 后 UIA 读取失败")
            return 1
        after_total = len(t1.splitlines())
        after_nb = count_nonblank(t1)
        print("after resize: total={} nonblank={}".format(after_total, after_nb))

        # 断言：resize 后非空白行不得明显多于基线（修复前 +28，修复后 0~1）
        grow = after_nb - base_nb
        if grow > 2:
            print("  [FAIL] resize 后非空白行 +{}（scrollback 增长，Bug B 复现）".format(grow))
            return 1
        print("  [PASS] resize 后非空白行变化 {}（无 scrollback 增长）".format(grow))
        print("SUMMARY: PASS (1 checks)")
        return 0
    finally:
        try:
            if mediator_proc:
                mediator_proc.kill()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        injector.cleanup(pid)


if __name__ == "__main__":
    sys.exit(run())
