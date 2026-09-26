# -*- coding: utf-8 -*-
r"""t_unload_tui_crash.py —— 【端到端】跑过 TUI 再卸载后，目标 shell 是否崩溃

用户现象（2026-09-26）
---------------------
  打开 WT → 劫持到新 WT → 运行 Textual TUI（taskboard.py）→ 卸载 → 回到旧 WT
  输入任意内容回车 → **旧 WT 的 pwsh 崩溃 `0xc0000005`（ACCESS_VIOLATION）**。
  补充：**不跑 TUI、劫持后直接卸载则不会崩**。

判据
----
  - 对照轨（不跑 TUI）：卸载后敲命令，目标 shell 存活
  - 触发轨（跑过 TUI）：卸载后敲命令，目标 shell 是否崩溃
  两轨对照即可定位「跑过 TUI」这一前提条件留下的残余状态。

用法
----
    set TI_TUI_TARGET=<TUI 脚本路径>          # 必填，如 taskboard.py
    python tests/_probe/t_unload_tui_crash.py

环境变量：
    PWTERM_DIR      pywezterm 包目录（默认 <proj>/reference）
    TI_TUI_TARGET   要跑的 TUI 脚本（**必填**；未设置则 SKIP）
    TI_SHELL        目标 shell（默认 PS7 的 pwsh.exe）

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
PS7_EXE = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                       "PowerShell", "7", "".join(["p", "w", "s", "h", ".exe"]))
SHELL_EXE = os.environ.get("TI_SHELL") or PS7_EXE
TARGET_TUI = os.environ.get("TI_TUI_TARGET", "")

COLS, ROWS = 120, 30


def pump(p, t, sec):
    """读 pty → feed 终端 → 回写应答。外层 deadline，绝不死等。"""
    buf = b""
    deadline = time.time() + sec
    while time.time() < deadline:
        try:
            c = p.read(65536, timeout=0.15)
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


def wait_handshake(mlog, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with open(mlog, "r", encoding="utf-8", errors="ignore") as f:
                if "Handshake OK" in f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def alive(pid):
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def run_track(pywezterm, label, run_tui):
    """一条轨：起目标 shell + mediator；可选跑 TUI；卸载；敲命令；看是否崩。"""
    print("\n" + "=" * 78)
    print("轨道 {}：{}".format(label, "跑过 TUI 再卸载" if run_tui else "不跑 TUI 直接卸载"))
    print("=" * 78)
    t_pty = d_pty = None
    target_pid = 0
    try:
        t_pty = pywezterm.Pty(COLS, ROWS)
        t_term = pywezterm.Terminal(COLS, ROWS, scrollback=5000)
        target_pid, _ = t_pty.spawn([SHELL_EXE, "-NoLogo"])
        print("  [setup] 目标 shell pid = {} ({})".format(
            target_pid, os.path.basename(SHELL_EXE)))
        pump(t_pty, t_term, 4.0)

        d_pty = pywezterm.Pty(COLS, ROWS)
        d_term = pywezterm.Terminal(COLS, ROWS, scrollback=5000)
        d_pty.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])
        if not wait_handshake(ti_log_path(target_pid)):
            print("  [FAIL] 握手超时")
            return 1
        print("  [setup] 握手成功")
        pump(d_pty, d_term, 2.0)

        if run_tui:
            # 注意：命令不能带 CJK 路径 —— 本 harness 的输入链路
            # （ConPTY → ReadConsoleInputW → 字节转换）会丢掉非 ASCII 字符，
            # 导致 shell 报 ParserError。故把目标脚本复制到纯 ASCII 临时路径再跑。
            tui_run = os.path.join(os.environ.get("TEMP", "."), "ti_tui_target.py")
            with open(TARGET_TUI, "rb") as src, open(tui_run, "wb") as dst:
                dst.write(src.read())
            py_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "py.exe")
            # pwsh 里 `"exe" "arg"` 是语法错误，需要 `& ` 调用运算符；cmd 不需要
            prefix = "& " if "pwsh" in os.path.basename(SHELL_EXE).lower() else ""
            d_pty.write(list(('%s"%s" "%s"\r' % (prefix, py_exe, tui_run)).encode("utf-8")))
            mid = pump(d_pty, d_term, 8.0)
            print("  [run] TUI 运行 {} 字节；屏幕含 Traceback={}".format(
                len(mid), "Traceback" in d_term.text()))
            for ln in [x for x in d_term.text().split("\n") if x.strip()][:4]:
                print("        | {}".format(ln[:110]))
        else:
            pump(d_pty, d_term, 8.0)

        # ---- 卸载：关掉 mediator 所在终端 → 管道断开 → DLL 走卸载路径 ----
        d_pty.close()
        d_pty = None
        print("  [unload] 已关闭 mediator 终端，等待卸载 ...")
        after = pump(t_pty, t_term, 3.0)

        # 关键判定：卸载时 DLL 会把 session 缓冲 Replay 进目标 ConHost，
        # 若其中含鼠标上报序列，则**旧 WT 会因此开启鼠标上报**（用户现象的前提）。
        seqs = [b"\x1b[?1000h", b"\x1b[?1003h", b"\x1b[?1015h", b"\x1b[?1006h",
                b"\x1b[?1000l", b"\x1b[?1003l", b"\x1b[?1015l", b"\x1b[?1006l"]
        hits = [repr(s.decode("latin1")) for s in seqs if s in after]
        print("  [mouse] 卸载后目标终端收到鼠标序列: {}".format(
            ", ".join(hits) if hits else "（无）"))
        if hits:
            print("        → 旧 WT 会据此开启鼠标上报（用户现象的前提成立）")
            # 打印首个命中点的上下文，便于定位泄漏源
            first = min(after.find(s) for s in seqs if s in after)
            lo = max(0, first - 120)
            hi = min(len(after), first + 120)
            print("        [ctx] ...{}...".format(
                repr(after[lo:hi].decode("latin1"))))

        # ---- 在目标 shell 自己的终端里敲命令 ----
        t_pty.write(list(b"dir\r"))
        out = pump(t_pty, t_term, 4.0)
        time.sleep(0.5)
        still = alive(target_pid)
        screen = t_term.text()
        crash = re.search(r"0x[0-9A-Fa-f]{8}", screen) or "已退出进程" in screen
        print("  [check] 敲 'dir' 后：目标存活={} 屏幕含崩溃迹象={}".format(still, bool(crash)))
        for ln in [x for x in screen.split("\n") if x.strip()][-6:]:
            print("        | {}".format(ln[:110]))
        if still and not crash:
            print("  [PASS] 卸载后目标 shell 正常响应，未崩溃")
            return 0
        print("  [FAIL] 卸载后目标 shell 崩溃（复现用户现象）")
        return 1
    finally:
        for p in (d_pty, t_pty):
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


def main():
    try:
        import pywezterm
    except ImportError as e:
        print("[SKIP] 无法 import pywezterm ({}): {}".format(PWTERM_DIR, e))
        print("\nSUMMARY: UNSUPPORTED (pywezterm 不可用)")
        return 0
    if not os.path.exists(SHELL_EXE):
        print("[SKIP] 目标 shell 不存在: {}".format(SHELL_EXE))
        print("\nSUMMARY: UNSUPPORTED (用 TI_SHELL 指定目标 shell)")
        return 0
    if not TARGET_TUI or not os.path.exists(TARGET_TUI):
        print("[SKIP] 未指定 TUI 目标（TI_TUI_TARGET={!r}）".format(TARGET_TUI))
        print("\nSUMMARY: UNSUPPORTED (请用 TI_TUI_TARGET 指定一个 Textual TUI 脚本)")
        return 0

    print("pywezterm={} | shell={} | tui={}".format(
        pywezterm.version(), SHELL_EXE, TARGET_TUI))
    failures = 0
    failures += run_track(pywezterm, "A", run_tui=False)   # 对照：不跑 TUI
    failures += run_track(pywezterm, "B", run_tui=True)    # 触发：跑过 TUI
    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(main())
