# -*- coding: utf-8 -*-
r"""t_kickstart_effect.py —— 只注入、不发命令，抓 mediator 输出，看 KickStart 副作用

对照两种实现：
  · 若 KickStart 写 ENTER → mediator 应收到 "\n" + 重新打印的 prompt（多一行）
  · 若 KickStart 写 key-up → 应只有 LazyInit 重放的 prompt，无多余 "\n"
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


def main():
    shell = sys.argv[1] if len(sys.argv) > 1 else "pwsh"
    cmdline = sys.argv[2] if len(sys.argv) > 2 else None
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
            target_pid, _ = t_pty.spawn([PS7_EXE, "-NoLogo"])
        pump(t_pty, t0, 3.0)

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

        boot = pump(d_pty, t, 2.0)
        print("注入后 2s 输出 = {} 字节".format(len(boot)))
        print("repr:", repr(boot[-200:]))
        lines = [x.rstrip() for x in t.text().split("\n") if x.strip()]
        print("非空行 {} 行:".format(len(lines)))
        for i, ln in enumerate(lines[:6]):
            print("  {}| {!r}".format(i, ln[:80]))
        print("cursor =", t.cursor())

        if cmdline:
            d_pty.write(list((cmdline + "\r").encode("utf-8")))
            out = pump(d_pty, t, 6.0)
            print("\n发命令后输出 = {} 字节".format(len(out)))
            print("repr:", repr(out[:200]))
            lines = [x.rstrip() for x in t.text().split("\n") if x.strip()]
            print("非空行 {} 行:".format(len(lines)))
            for i, ln in enumerate(lines[:8]):
                print("  {}| {!r}".format(i, ln[:80]))
            print("cursor =", t.cursor())
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
