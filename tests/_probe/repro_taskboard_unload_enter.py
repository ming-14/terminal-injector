#!/usr/bin/env python3
"""复现现场：劫持后跑 taskboard TUI → 卸载 → 回旧终端**按回车** → pwsh 崩溃。

用户现场（2026-09-29 报告，全程真 WT）：

    PS C:\\Users\\rikka> C:\\...\\tests\\live\\textual\\taskboard.py
    PS C:\\Users\\rikka>
    [已退出进程，代码为 3221225477 (0xc0000005)]
    现在可以使用Ctrl+D关闭此终端，或按 Enter 重新启动。

关键点：**卸载后按 Enter 就崩**（不是敲 echo）。已有的
`tests/e2e_v2/lifecycle/test_unload_tui_shell_alive.py` 场景 B 只验到
"卸载后敲 echo / 连续交互"，**没有单独验"回车"这一步**，所以拿它复现不出
用户现场那条"按 Enter 重启"的路径。

本探针全程 pywezterm（不起真 WT）：

    源终端  = pywezterm.Pty 跑 pwsh            （扮演"旧 wt"）
    新终端  = pywezterm.Pty 跑 mediator         （扮演"劫持后新开的终端"）
    注入    = mediator --target-pid <源 pwsh pid>
    TUI     = 在**新终端**里跑 taskboard.py

然后：关新终端（卸载）→ 在**源终端**依次注入 Enter / 其他键，逐步隔离
"到底是哪个键触发的崩溃"。

用法：
    python tests/_probe/repro_taskboard_unload_enter.py
    python tests/_probe/repro_taskboard_unload_enter.py --keep-going  # 崩溃后继续探测
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


def alive(pid: int) -> bool:
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def probe_enter_after_unload(s, shell_pid: int, src_lane: str = "src") -> dict:
    """卸载已完成后：在源终端按 Enter，看 shell 是否崩。

    返回 {stage: alive_bool}。
    """
    out = {}

    def check(tag: str) -> bool:
        ok = alive(shell_pid)
        out[tag] = ok
        print("    [{:>4}] pid={} alive={}".format(
            "OK" if ok else "DEAD", shell_pid, ok))
        return ok

    check("卸载后立刻")

    # ── 关键步骤：只按一个回车（现场就是这一步崩的）────────────────
    print("    --> 注入单个 Enter（现场崩溃点）")
    s.press("Enter", lane=src_lane)
    time.sleep(1.5)
    check("按 Enter 后")

    if not out["按 Enter 后"]:
        return out

    # 再按一次（现场提示"或按 Enter 重新启动"，可能第一次只是没反应）
    print("    --> 再注入一个 Enter")
    s.press("Enter", lane=src_lane)
    time.sleep(1.5)
    check("再按 Enter 后")

    if not out["再按 Enter 后"]:
        return out

    # 敲一条命令看还能不能干活
    print("    --> 敲 echo 探测")
    tok = "rp{}".format(int(time.time()) % 100000)
    s.send_line("echo {}".format(tok), lane=src_lane)
    time.sleep(2.0)
    check("敲 echo 后")
    out["echo_seen"] = tok in s.text(src_lane)

    return out


def run_once(host: str, quit_tui: bool) -> int:
    print("\n" + "=" * 72)
    print("host={}  quit_tui={}  （源终端 = pywezterm.Pty，新终端 = pywezterm.Pty）"
          .format(host, quit_tui))
    print("=" * 72)

    missing = paths.preflight()
    if missing:
        print("  前提缺失: {}".format("; ".join(missing)))
        return 1

    failures = 0
    src_lane = "src" if host == "conpty" else "dst"

    with PwSession(host=host, cols=COLS, rows=ROWS, src_size=(COLS, ROWS),
                   target_args=[PS7_EXE, "-NoLogo"], call_prefix="&") as s:
        shell_pid = s.target_pid
        print("  源 shell pid = {}".format(shell_pid))

        if not s.spawn_mediator():
            print("  [FAIL] 握手失败；mediator 日志尾部:")
            for line in s.mediator_log().splitlines()[-15:]:
                print("        ", line)
            return 1
        print("  [PASS] 握手成功；拓扑 = {}".format(s.topology()))

        # 源 shell 就绪
        s.wait_stable(quiet=0.6, timeout=6.0, lane=src_lane)

        # ── 现场步骤：在新终端里跑 taskboard.py ─────────────────────
        py = sys.executable
        s.shell_run([py, TUI_SCRIPT], lane="dst")
        print("  已在新终端启动 taskboard（{}）".format(TUI_SCRIPT))

        if not s.wait_screen(TUI_TITLE, timeout=45.0, lane="dst"):
            print("  [FAIL] 新终端未见 TUI 标题；屏幕尾部:")
            for line in s.tail_lines(10, lane="dst"):
                print("        ", repr(line))
            return 1
        print("  [PASS] TUI 在新终端渲染出来")
        s.wait_stable(quiet=1.0, timeout=10.0, lane="dst")

        if quit_tui:
            # 现场那串输出里 TUI 是**已经退掉**了的（屏幕回到 PS 提示符），
            # 所以这个分支才是对照现场。
            s.press("Tab", lane="dst")
            time.sleep(0.8)
            s.write("q", lane="dst")
            deadline = time.time() + 20.0
            while time.time() < deadline and TUI_TITLE in s.text("dst"):
                time.sleep(0.3)
            gone = TUI_TITLE not in s.text("dst")
            print("  [{}] TUI 已退出（Tab+q）".format("PASS" if gone else "FAIL"))
            if not gone:
                print("        屏幕尾部:", s.tail_lines(6, lane="dst"))
                return 1
            s.wait_stable(quiet=1.0, timeout=8.0, lane="dst")
        else:
            print("  [INFO] TUI 保持运行，直接卸载")

        # ── 卸载 ────────────────────────────────────────────────
        print("\n  ── 卸载：关闭新终端（mediator 退出 → 管道断 → DLL 卸载）")
        s.close_mediator()
        time.sleep(2.5)
        print("  [PASS] 已卸载")
        print("     mediator 日志尾部:")
        for line in s.mediator_log().splitlines()[-8:]:
            print("        ", line)

        # ── 回到源终端：按 Enter ─────────────────────────────────
        print("\n  ── 回到源终端（{} 通道）操作 ──".format(src_lane))
        print("     源终端当前屏幕尾部:")
        for line in s.tail_lines(12, lane=src_lane):
            print("        ", repr(line))

        stages = probe_enter_after_unload(s, shell_pid, src_lane=src_lane)

        print("\n     源终端屏幕尾部（探测后）:")
        for line in s.tail_lines(12, lane=src_lane):
            print("        ", repr(line))

        dead = [k for k, v in stages.items() if not v]
        if dead:
            print("\n  [REPRO] 源 shell 崩溃，触发点 = {}".format(dead[0]))
            failures += 1
        else:
            print("\n  [OK] 全套操作后源 shell 仍存活（未复现）")

    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="conpty", choices=["conpty", "conhost"],
                    help="conpty=源终端也是 Pty（更贴近真 WT，默认）")
    ap.add_argument("--scenario", default="both",
                    choices=["both", "quit", "alive"])
    args = ap.parse_args()

    pyterm.require()
    try:
        import psutil  # noqa: F401
    except ImportError:
        print("缺少 psutil：pip install psutil")
        return 1
    if not os.path.exists(TUI_SCRIPT):
        print("缺少 TUI 脚本 {}".format(TUI_SCRIPT))
        return 1

    cases = {"both": [True, False], "quit": [True], "alive": [False]}[args.scenario]
    total = 0
    for quit_tui in cases:
        total += run_once(args.host, quit_tui)

    print("\n" + "=" * 72)
    print("总计 failures = {}".format(total))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
