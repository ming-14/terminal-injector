# -*- coding: utf-8 -*-
r"""t_child_dsr_clobber.py —— 验证：子进程的 DSR 应答不再污染父进程光标

真机日志证据（用户注入 pwsh 后跑 TUI 子进程）：
    t=11.05s InjectDllToChild: success pid=11840
    t=11.71s ApplyWtCursorReport: cursor=(0,0) (from VT 1,1)   ← 子进程查询的应答改了父进程
    t=12.92s WriteConsoleW_Detour len=19 → 从 (0,0) 写到 (19,0)  ← 父进程 prompt 落到第 0 行

本探针让子进程做同样的事：把光标归左上角并发 DSR 查询（ESC[H ESC[6n），
然后看父进程 prompt 是否还在原行。

    python t_child_dsr_clobber.py
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
PY = sys.executable
CHILD = os.path.join(HERE, "_dsrchild.py")
COLS, ROWS = 120, 30
MARK = "ECHO_AFTER_DSR"


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


def dump(t, label, limit=10):
    print("\n--- {} ---".format(label))
    rows = t.text().split("\n")
    for i, ln in enumerate(rows[:limit]):
        print("  r{:<2}| {!r}".format(i, ln.rstrip()[:88]))
    print("  cursor =", t.cursor())


def main():
    with open(CHILD, "w", encoding="utf-8") as f:
        f.write('import sys, time\n'
                'sys.stdout.write("\\x1b[H\\x1b[6n")\n'   # 归左上角 + 查光标位置
                'sys.stdout.flush()\n'
                'time.sleep(2.5)\n')

    t_pty = d_pty = None
    target_pid = 0
    try:
        t_pty = pywezterm.Pty(COLS, ROWS)
        t0 = pywezterm.Terminal(COLS, ROWS, scrollback=5000)
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
        pump(d_pty, t, 2.0)
        dump(t, "注入后")

        # 跑子进程：ESC[H + ESC[6n（子进程的 DSR 查询）
        d_pty.write(list(('& "{}" "{}"\r'.format(PY, CHILD)).encode("utf-8")))
        pump(d_pty, t, 5.0)
        dump(t, "子进程(DSR 查询)退出后")

        # 输入标记，看回显落在哪一行
        d_pty.write(list(MARK.encode("utf-8")))
        out = pump(d_pty, t, 2.0)
        print("\n[回显] {} 字节: {!r}".format(len(out), out[:120]))
        dump(t, "输入 {} 后".format(MARK))

        log = ti_log_path(target_pid)
        try:
            with open(log, "r", encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            print("\n[日志] WtStateReport / ApplyWtCursorReport:")
            for ln in txt.splitlines():
                if "WtStateReport" in ln or "ApplyWtCursorReport" in ln:
                    i = ln.find("]")
                    print("   " + (ln[i + 1:] if i >= 0 else ln).strip()[:130])
        except OSError:
            pass
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
