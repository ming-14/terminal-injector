# -*- coding: utf-8 -*-
r"""t_textual_asyncio_freeze.py —— 【端到端】注入后跑 Textual 应用，画面是否定格

背景（2026-09-26）
------------------
用户报告：旧 WT 里劫持 pwsh → 新 WT 里跑 `taskboard.py`（Textual TUI）→ 有画面但
**鼠标完全失效、画面不刷新**（约数秒后进程退出/冻结）；不注入则完全正常。

根因：`IsInputHandleSlow` / `IsConsoleHandle` 为判定句柄类型，会对**任意句柄**调
`GetNumberOfConsoleInputEvents` / `GetFileType`；对非控制台句柄（CPython 锁句柄、
asyncio 内部句柄等）失败并污染**当前线程的 `GetLastError`** 为 `ERROR_INVALID_HANDLE(6)`。
这两个判定在 `WaitForSingleObject(Ex)` / `ReadFile` 等 Detour 内被高频调用 →
`asyncio` 在 `IocpProactor._poll` 抛 `OSError [WinError 6]` → 事件循环死亡 → 画面定格。

判据
----
注入轨与不注入轨都应满足：**屏幕在静置期间仍在变化**（Textual 定时器每秒重绘），
且**发鼠标事件后屏幕变化**。修复前注入轨两者皆 False（画面定格）。

用法
----
    TI_TUI_TARGET=<TUI 脚本路径> python tests/_probe/t_textual_asyncio_freeze.py

环境变量：
    PWTERM_DIR      pywezterm 包目录（默认 <proj>/reference）
    TI_TUI_TARGET   要跑的 TUI 脚本（**必填**；未设置则 SKIP —— 按探针约定不内置个人路径）

只读：不改工程源码。
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "tests", "e2e"))

from common.paths import BUILD_BIN, ti_log_path   # noqa: E402

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)

MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
LOG_DIR = os.path.join(BUILD_BIN, "logs")
COMSPEC = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "cmd.exe")
LAUNCHER = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "py.exe")
TARGET = os.environ.get("TI_TUI_TARGET", "")

COLS, ROWS = 120, 30


def pump(p, t, sec):
    """读 pty → feed 终端 → 回写应答（DSR 等），带外层 deadline。"""
    buf = b""
    end = time.time() + sec
    while time.time() < end:
        try:
            c = p.read(65536, timeout=0.1)
        except Exception:
            break
        if c:
            buf += c
            try:
                t.feed(c)
                r = t.drain_written()
                if r:
                    p.write(r)
            except Exception:
                pass
    return buf


def wait_log(path, needle, timeout):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                if needle in f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def newest_child_log(exclude_pids):
    cands = []
    for x in os.listdir(LOG_DIR):
        if not (x.startswith("injected_") and x.endswith(".log")):
            continue
        m = re.search(r"injected_(\d+)_", x)
        if m and int(m.group(1)) in exclude_pids:
            continue
        p = os.path.join(LOG_DIR, x)
        cands.append((os.path.getmtime(p), p))
    cands.sort(reverse=True)
    return cands[0][1] if cands else None


def cleanup(pair, target_pid):
    for p in pair:
        try:
            if p is not None:
                p.close()
        except Exception:
            pass
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info["name"] or "").lower() == "terminal_injector.exe":
                    proc.terminate()
            except Exception:
                pass
        if target_pid:
            try:
                psutil.Process(target_pid).terminate()
            except Exception:
                pass
    except Exception:
        pass


def run_injected(pywezterm):
    """注入轨：cmd 目标 + mediator（冒充 WT）+ 在目标终端里跑 TUI。"""
    ta = tb = None
    target_pid = 0
    try:
        ta = pywezterm.Pty(150, 40)
        ta_term = pywezterm.Terminal(150, 40)
        target_pid, _ = ta.spawn([COMSPEC])
        pump(ta, ta_term, 2.5)

        tb = pywezterm.Pty(COLS, ROWS)
        tb_term = pywezterm.Terminal(COLS, ROWS, scrollback=3000)
        tb.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])
        if not wait_log(ti_log_path(target_pid), "Handshake OK", 25):
            print("  [FAIL] 握手超时")
            return 1
        print("  [PASS] 握手成功（目标 pid={}）".format(target_pid))
        pump(tb, tb_term, 1.5)

        tb.write(list(('"%s" "%s"\r' % (LAUNCHER, TARGET)).encode("utf-8")))
        pump(tb, tb_term, 8.0)

        screen1 = tb_term.text()
        if "Traceback" in screen1:
            print("  [FAIL] TUI 崩溃（屏幕含 Traceback）—— 见 {} 与 {}".format(
                os.path.basename(newest_child_log({target_pid}) or "?"),
                os.path.basename(ti_log_path(target_pid))))
            return 1
        print("  [PASS] TUI 已渲染，无崩溃栈")

        pump(tb, tb_term, 6.0)
        screen2 = tb_term.text()
        if screen1 != screen2:
            print("  [PASS] 静置期间画面仍在更新（定时器重绘）")
        else:
            print("  [FAIL] 静置期间画面无变化 —— 事件循环已停摆（画面定格）")
            return 1

        tb.write(list(b"\x1b[<35;5;5M"))
        pump(tb, tb_term, 0.5)
        tb.write(list(b"\x1b[<0;5;5M\x1b[<0;5;5m"))
        pump(tb, tb_term, 3.0)
        if tb_term.text() != screen2:
            print("  [PASS] 发鼠标事件后画面变化（鼠标链路生效）")
        else:
            print("  [FAIL] 发鼠标事件后画面无变化")
            return 1
        return 0
    finally:
        cleanup((tb, ta), target_pid)


def run_control(pywezterm):
    """对照轨：不注入，直接在 ConPTY 里跑同一个 TUI（应正常渲染）。"""
    p = None
    try:
        p = pywezterm.Pty(COLS, ROWS)
        t = pywezterm.Terminal(COLS, ROWS, scrollback=3000)
        p.spawn([LAUNCHER, TARGET])
        pump(p, t, 9.0)
        txt = t.text()
        ok = ("Traceback" not in txt) and any(x.strip() for x in txt.split("\n"))
        print("  [{}] 不注入直跑：{}".format("PASS" if ok else "FAIL",
                                            "正常渲染" if ok else "异常/空白"))
        return 0 if ok else 1
    finally:
        cleanup((p,), 0)


def main():
    try:
        import pywezterm
    except ImportError as e:
        print("[SKIP] 无法 import pywezterm ({}): {}".format(PWTERM_DIR, e))
        print("\nSUMMARY: UNSUPPORTED (pywezterm 不可用)")
        return 0

    if not TARGET or not os.path.exists(TARGET):
        print("[SKIP] 未指定 TUI 目标（TI_TUI_TARGET={!r}）".format(TARGET))
        print("\nSUMMARY: UNSUPPORTED (请用 TI_TUI_TARGET 指定一个 Textual TUI 脚本)")
        return 0

    print("pywezterm = {} | target = {}".format(pywezterm.version(), TARGET))
    failures = 0

    print("\n[对照轨] 不注入")
    failures += run_control(pywezterm)

    print("\n[注入轨] cmd 目标 + mediator 冒充 WT")
    failures += run_injected(pywezterm)

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(main())
