#!/usr/bin/env python3
"""扫"卸载 → 首次输入"的时间间隔，并抓崩溃退出码。

前两轮探针发现：
  - 只按 Enter（空行）→ 多数不崩
  - 卸载后**有实际输入动作**时崩，但**不稳定**（同一个 `echo` 用例
    一次崩一次不崩）⇒ 触发条件里还有没控住的变量。

最大嫌疑 = **卸载后到首次输入之间的时间差**（卸载是异步的：管道断开 →
DLL 收尾 → 目标真的回到原生输入循环，这个窗口里送输入才撞得上）。

本探针固定动作 = `echo <token>` + 回车（已证能崩），只扫 settle 时间：
    0.0s / 0.5s / 1.0s / 2.0s / 4.0s / 8.0s

并记录崩溃时的进程退出码（Windows 上 psutil 给的是原始 NTSTATUS 无符号值，
`3221225477 == 0xC0000005`）。

用法：
    python tests/_probe/scan_unload_settle.py
    python tests/_probe/scan_unload_settle.py --settles 0,1,2 --repeat 3
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

NTSTATUS = {
    0xC0000005: "0xc0000005 (ACCESS_VIOLATION)",
    0xC0000409: "0xc0000409 (STACK_BUFFER_OVERRUN / fail-fast)",
    0xC0000374: "0xc0000374 (HEAP_CORRUPTION)",
}


def fmt_code(code):
    if code is None:
        return "None"
    if code in NTSTATUS:
        return NTSTATUS[code]
    return "{} (0x{:08X})".format(code, code & 0xFFFFFFFF)


def start_session():
    return PwSession(host="conpty", cols=COLS, rows=ROWS, src_size=(COLS, ROWS),
                     target_args=[PS7_EXE, "-NoLogo"], call_prefix="&")


def prep(s):
    """握手 → 新终端跑 TUI → 退出 TUI → 卸载。返回 src 通道名。"""
    src = "src"
    if not s.spawn_mediator():
        raise RuntimeError("握手失败")
    s.wait_stable(quiet=0.6, timeout=6.0, lane=src)
    s.shell_run([sys.executable, TUI_SCRIPT], lane="dst")
    if not s.wait_screen(TUI_TITLE, timeout=45.0, lane="dst"):
        raise RuntimeError("TUI 未渲染")
    s.wait_stable(quiet=1.0, timeout=10.0, lane="dst")
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
    return src


def one_run(settle: float) -> dict:
    """跑一次：卸载后等 settle 秒，然后 echo+CR，看崩不崩。"""
    import psutil
    s = start_session()
    rec = {"settle": settle, "result": "?", "code": None, "detail": ""}
    with s:
        pid = s.target_pid
        try:
            src = prep(s)
        except RuntimeError as e:
            rec["result"] = "SKIP"
            rec["detail"] = str(e)
            return rec
        # 卸载完成后计时
        t0 = time.time()
        time.sleep(settle)
        rec["real_settle"] = round(time.time() - t0, 2)

        proc = psutil.Process(pid)
        if not proc.is_running():
            rec["result"] = "DIED_BEFORE_INPUT"
            try:
                rec["code"] = proc.wait(timeout=0)
            except Exception:
                pass
            return rec

        s.send_line("echo probe_{}".format(int(time.time()) % 10000), lane=src)
        # 等最多 6s 看它死没死
        try:
            code = proc.wait(timeout=6.0)
            rec["result"] = "REPRO"
            rec["code"] = code
        except psutil.TimeoutExpired:
            rec["result"] = "OK"
        except Exception:
            rec["result"] = "OK"
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settles", default="0,0.5,1,2,4,8",
                    help="逗号分隔的等待秒数")
    ap.add_argument("--repeat", type=int, default=2,
                    help="每个 settle 重复次数")
    args = ap.parse_args()

    pyterm.require()
    if not os.path.exists(TUI_SCRIPT):
        print("缺少 TUI 脚本")
        return 1

    settles = [float(x) for x in args.settles.split(",") if x.strip()]

    print("=" * 76)
    print("卸载后 settle × echo+CR  → 崩溃矩阵")
    print("=" * 76)
    rows = []
    for st in settles:
        for i in range(args.repeat):
            r = one_run(st)
            rows.append(r)
            print("  settle={:>4}s run#{} -> {:<20} code={} {}".format(
                r["settle"], i + 1, r["result"], fmt_code(r["code"]),
                ("(" + r["detail"] + ")") if r["detail"] else ""))

    print("\n" + "=" * 76)
    print("汇总")
    print("=" * 76)
    print("  {:<8} {:<6} {}".format("settle", "REPRO", "总数"))
    for st in settles:
        sub = [r for r in rows if r["settle"] == st]
        n = sum(1 for r in sub if r["result"] == "REPRO")
        print("  {:<8} {:<6} {}".format(st, n, len(sub)))

    repro = [r for r in rows if r["result"] == "REPRO"]
    if repro:
        codes = sorted({fmt_code(r["code"]) for r in repro})
        print("\n  崩溃退出码：{}".format(", ".join(codes)))
    else:
        print("\n  未复现崩溃")
    return 0


if __name__ == "__main__":
    sys.exit(main())
