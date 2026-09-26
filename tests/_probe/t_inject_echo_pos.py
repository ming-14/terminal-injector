# -*- coding: utf-8 -*-
r"""t_inject_echo_pos.py —— 复现「注入后输入回显落错行」

真机现象（官方 WT，pwsh 带 banner）：
    PowerShell 7.x > <输入的字符落在这里>      ← 回显落在 banner 行，而非 prompt 行
    ...
    PS C:\Users\<user>> <命令>
    PS C:\Users\<user>>

与既有探针的差异：既有探针用 `pwsh -NoLogo`（无 banner，目标屏只有 1 行 prompt），
真机 pwsh 有 banner + 更新提示（多行）。本探针参数化 banner，并打印**完整屏幕
（含空行 + 行号）**，看输入回显落在哪一行。

    python t_inject_echo_pos.py pwsh logo      # 带 banner（真机场景）
    python t_inject_echo_pos.py pwsh           # 不带 banner（既有探针场景）
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "e2e"))
from common.paths import BUILD_BIN, PROJECT_ROOT, ti_log_path   # noqa: E402

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)
import pywezterm  # noqa: E402

MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
PS7_EXE = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                       "PowerShell", "7", "".join(["p", "w", "s", "h", ".exe"]))
COLS, ROWS = 120, 30
MARK = "ECHO_POS_MARK"


def pump(p, t, sec):
    buf = b""
    end = time.time() + sec
    while time.time() < end:
        try:
            c = p.read(65536, timeout=0.05)
        except Exception:
            break
        if c:
            buf += c
            t.feed(c)
            try:
                r = t.drain_written()
                if r:
                    p.write(r)
            except Exception:
                pass
    return buf


def dump_screen(t, label, limit=12):
    print("\n--- {} (完整屏幕, 含空行) ---".format(label))
    try:
        rows = t.text().split("\n")
    except Exception:
        rows = []
    shown = 0
    for i, ln in enumerate(rows):
        if shown >= limit:
            break
        s = ln.rstrip()
        print("  r{:<2}| {!r}".format(i, s[:90]))
        shown += 1
    print("  cursor =", t.cursor())


def main():
    shell = sys.argv[1] if len(sys.argv) > 1 else "pwsh"
    logo = len(sys.argv) > 2 and sys.argv[2] == "logo"
    # 第 3 参：注入前等目标启动的毫秒数（默认 3000；调小 = 趁目标还在启动时注入）
    delay_ms = int(sys.argv[3]) if len(sys.argv) > 3 else 3000

    t_pty = d_pty = None
    target_pid = 0
    try:
        t_pty = pywezterm.Pty(COLS, ROWS)
        t0 = pywezterm.Terminal(COLS, ROWS, scrollback=5000)
        if shell == "cmd":
            cmd_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                                   "System32", "cmd.exe")
            target_pid, _ = t_pty.spawn([cmd_exe])
        else:
            argv = [PS7_EXE] if logo else [PS7_EXE, "-NoLogo"]
            target_pid, _ = t_pty.spawn(argv)
        pump(t_pty, t0, delay_ms / 1000.0)
        print("[setup] 目标 {} pid={} (logo={}, 注入前等待 {}ms)".format(
            shell, target_pid, logo, delay_ms))
        dump_screen(t0, "注入前 目标屏")

        d_pty = pywezterm.Pty(COLS, ROWS)
        t = pywezterm.Terminal(COLS, ROWS, scrollback=5000)
        d_pty.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])
        t1 = time.time()
        while time.time() - t1 < 20:
            try:
                with open(ti_log_path(target_pid), "r",
                          encoding="utf-8", errors="ignore") as f:
                    if "Handshake OK" in f.read():
                        break
            except OSError:
                pass
            time.sleep(0.2)

        pump(d_pty, t, 2.0)
        dump_screen(t, "注入后")

        d_pty.write(list(MARK.encode("utf-8")))
        out = pump(d_pty, t, 2.0)
        print("\n[输入回显] 输出 {} 字节: {!r}".format(len(out), out[:160]))
        dump_screen(t, "输入 {} 后".format(MARK))
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


if __name__ == "__main__":
    main()
