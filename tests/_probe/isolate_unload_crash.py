#!/usr/bin/env python3
"""隔离"卸载后源 shell 崩溃"的触发条件（全程 pywezterm）。

第一轮探针（repro_taskboard_unload_enter.py）已复现崩溃，但触发点是
**敲了一条非空的 `echo`**，而不是"按 Enter"。本探针逐项隔离变量：

    A. 只按 Enter（空行提交）        —— 是否崩？
    B. 只敲一个字符，不回车          —— 是否崩？
    C. 敲 `echo x` + 回车            —— 是否崩？（已知崩）
    D. 敲上去再擦掉（Backspace）后回车 —— 是否崩？
    E. 卸载后先等更久再操作          —— 是否崩？

同时抓崩溃退出码（`psutil.Process.wait()` / exitcode），确认是不是
`0xc0000005`（3221225477）。

每个用例独立起会话（互不污染）。

用法：
    python tests/_probe/isolate_unload_crash.py
    python tests/_probe/isolate_unload_crash.py --cases A,B
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.normpath(os.path.join(HERE, "..", "e2e_v2"))
sys.path.insert(0, V2)

from pwcommon import paths           # noqa: E402
from pwcommon import pyterm          # noqa: E402
from pwcommon.session import PS7_EXE, PwSession   # noqa: E402

COLS, ROWS = 120, 30
TUI_SCRIPT = os.path.join(paths.PROJECT_ROOT, "tests", "live", "textual", "taskboard.py")
TUI_TITLE = "TaskBoard"


def shell_state(shell_pid: int):
    """返回 (alive, exitcode)。exitcode 只有在进程已结束时才有效。"""
    import psutil
    try:
        p = psutil.Process(shell_pid)
        code = p.wait(timeout=0) if not p.is_running() else None
        return p.is_running(), code
    except psutil.TimeoutExpired:
        return True, None
    except Exception:
        return False, None


def prep(s, quit_tui: bool, settle: float = 0.0):
    """共同前置：握手 → 新终端跑 TUI →（可选退出）→ 卸载。"""
    src = "src"
    if not s.spawn_mediator():
        raise RuntimeError("握手失败")
    s.wait_stable(quiet=0.6, timeout=6.0, lane=src)

    s.shell_run([sys.executable, TUI_SCRIPT], lane="dst")
    if not s.wait_screen(TUI_TITLE, timeout=45.0, lane="dst"):
        raise RuntimeError("TUI 未渲染")
    s.wait_stable(quiet=1.0, timeout=10.0, lane="dst")

    if quit_tui:
        s.press("Tab", lane="dst")
        time.sleep(0.8)
        s.write("q", lane="dst")
        deadline = time.time() + 20.0
        while time.time() < deadline and TUI_TITLE in s.text("dst"):
            time.sleep(0.3)
        if TUI_TITLE in s.text("dst"):
            raise RuntimeError("TUI 未退出")
        s.wait_stable(quiet=1.0, timeout=8.0, lane="dst")

    s.close_mediator()
    time.sleep(2.5)
    if settle:
        time.sleep(settle)
    return src


def case_A(s, pid):   # 只按 Enter
    src = prep(s, quit_tui=True)
    for i in range(3):
        s.press("Enter", lane=src)
        time.sleep(1.2)
        a, _ = shell_state(pid)
        print("      Enter #{} -> alive={}".format(i + 1, a))
        if not a:
            return "Enter #{}".format(i + 1)
    return None


def case_B(s, pid):   # 敲字符不回车
    src = prep(s, quit_tui=True)
    for ch in ("a", "1", " "):
        s.write(ch, lane=src)
        time.sleep(1.0)
        a, _ = shell_state(pid)
        print("      write {!r} -> alive={}".format(ch, a))
        if not a:
            return "write {!r}".format(ch)
    return None


def case_C(s, pid):   # echo + 回车
    src = prep(s, quit_tui=True)
    s.send_line("echo probe_c", lane=src)
    time.sleep(2.0)
    a, _ = shell_state(pid)
    print("      echo+CR -> alive={}".format(a))
    return None if a else "echo+CR"


def case_D(s, pid):   # 敲上去擦掉再回车
    src = prep(s, quit_tui=True)
    s.write("zzz", lane=src)
    time.sleep(0.8)
    for _ in range(3):
        s.press("Backspace", lane=src)
        time.sleep(0.2)
    s.press("Enter", lane=src)
    time.sleep(1.5)
    a, _ = shell_state(pid)
    print("      type+3BS+CR -> alive={}".format(a))
    return None if a else "type+3BS+CR"


def case_E(s, pid):   # 卸载后等更久
    src = prep(s, quit_tui=True, settle=8.0)
    s.press("Enter", lane=src)
    time.sleep(1.5)
    a, _ = shell_state(pid)
    print("      settle8s+CR -> alive={}".format(a))
    if not a:
        return "settle8s+CR"
    s.send_line("echo probe_e", lane=src)
    time.sleep(2.0)
    a, _ = shell_state(pid)
    print("      settle8s+echo+CR -> alive={}".format(a))
    return None if a else "settle8s+echo+CR"


CASES = {"A": case_A, "B": case_B, "C": case_C, "D": case_D, "E": case_E}


def run_case(key, host):
    print("\n" + "=" * 72)
    print("用例 {} : host={}".format(key, host))
    print("=" * 72)
    with PwSession(host=host, cols=COLS, rows=ROWS, src_size=(COLS, ROWS),
                   target_args=[PS7_EXE, "-NoLogo"], call_prefix="&") as s:
        pid = s.target_pid
        print("  源 shell pid = {}".format(pid))
        try:
            hit = CASES[key](s, pid)
        except RuntimeError as e:
            print("  [SKIP] 前置失败: {}".format(e))
            return "SKIP"
        if hit:
            print("  [REPRO] 崩溃触发点 = {}".format(hit))
            return "REPRO"
        print("  [OK] 未崩")
        return "OK"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="conpty", choices=["conpty", "conhost"])
    ap.add_argument("--cases", default="A,B,C,D,E")
    args = ap.parse_args()

    pyterm.require()
    if not os.path.exists(TUI_SCRIPT):
        print("缺少 TUI 脚本")
        return 1

    keys = [k.strip().upper() for k in args.cases.split(",") if k.strip()]
    results = {}
    for k in keys:
        if k not in CASES:
            print("未知用例 {!r}".format(k))
            continue
        results[k] = run_case(k, args.host)

    print("\n" + "=" * 72)
    for k in keys:
        if k in results:
            print("  用例 {} -> {}".format(k, results[k]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
