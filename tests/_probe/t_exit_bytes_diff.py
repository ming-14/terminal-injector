# -*- coding: utf-8 -*-
r"""t_exit_bytes_diff.py —— 双轨「退出瞬间」的**原始字节流**逐块对照

目的
----
t_shell_child_exit.py 已证明：pwsh + opentui，Ctrl+C 后
  原生屏幕 2 行（干净），注入屏幕 3 行（多了 'PS ... v=1,a=q,t=d,f=24;AAAA'）。
本探针要拿到**引起这个差异的确切字节**：

  轨道 A 原生：pywezterm.Pty 直接跑 pwsh → child，Ctrl+C 后 read() 的字节
  轨道 B 注入：mediator（冒充 WT）→ ConPTY → 注入的 pwsh → child，
               Ctrl+C 后 d_pty.read() 的字节（= 最终 WT 会看到的东西）

两轨的**退出字节流**做对齐 diff：找到第一处分叉，就是 bug 注入点。

不猜、不简化：原始字节直接落盘 + 打印。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "e2e"))
from common.paths import BUILD_BIN, PROJECT_ROOT, ti_log_path   # noqa: E402
from common import childlog                                     # noqa: E402

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)
import pywezterm  # noqa: E402

MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
PS7_EXE = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                       "PowerShell", "7", "".join(["p", "w", "s", "h", ".exe"]))
# 目标全屏 TUI 由环境变量指定（无默认值，避免写死本机路径）
CHILD = os.environ.get("TI_TUI_TARGET", "")
COLS, ROWS = 120, 30
CTRL_C = b"\x03"
CHILD_CMD = '& "{}"\r'.format(CHILD)


def pump(p, t, sec):
    buf = b""
    deadline = time.time() + sec
    while time.time() < deadline:
        try:
            c = p.read(65536, timeout=0.15)
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


def run_track(pywezterm, inject):
    """返回 (boot, mid, post) 三段原始字节。"""
    t_pty = d_pty = None
    target_pid = 0
    try:
        t_pty = pywezterm.Pty(COLS, ROWS)
        t_target = pywezterm.Terminal(COLS, ROWS, scrollback=5000)
        target_pid, _ = t_pty.spawn([PS7_EXE, "-NoLogo"])
        pump(t_pty, t_target, 3.5)

        if inject:
            d_pty = pywezterm.Pty(COLS, ROWS)
            t = pywezterm.Terminal(COLS, ROWS, scrollback=5000)
            d_pty.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])
            t0 = time.time()
            while time.time() - t0 < 25:
                try:
                    with open(ti_log_path(target_pid), "r",
                              encoding="utf-8", errors="ignore") as f:
                        if "Handshake OK" in f.read():
                            break
                except OSError:
                    pass
                time.sleep(0.2)
            boot = pump(d_pty, t, 2.0)
            d_pty.write(list(CHILD_CMD.encode("utf-8")))
            mid = pump(d_pty, t, 10.0)
            d_pty.write(list(CTRL_C))
            post = pump(d_pty, t, 6.0)
            return boot, mid, post, t
        else:
            boot = b""
            t_pty.write(list(CHILD_CMD.encode("utf-8")))
            mid = pump(t_pty, t_target, 10.0)
            t_pty.write(list(CTRL_C))
            post = pump(t_pty, t_target, 6.0)
            return boot, mid, post, t_target
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


def show(label, b):
    print("\n--- {} ({} B) ---".format(label, len(b)))
    print(repr(b[-600:]))


def first_diff(a, b):
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    if len(a) != len(b):
        return n
    return -1


def main():
    if not CHILD or not os.path.exists(CHILD):
        print("[SKIP] 需设置 TI_TUI_TARGET 指向一个全屏 TUI 可执行文件")
        return 2
    print("pywezterm =", pywezterm.version())
    nat = run_track(pywezterm, inject=False)
    inj = run_track(pywezterm, inject=True)

    show("原生 post(Ctrl+C 后)", nat[2])
    show("注入 post(Ctrl+C 后)", inj[2])

    print("\n=== 命令回显段 mid[:360] ===")
    print("native mid[:360] =", repr(nat[1][:360]))
    print()
    print("inject mid[:360] =", repr(inj[1][:360]))

    # 落盘
    try:
        with open(os.path.join(HERE, "_exit_native.bin"), "wb") as f:
            f.write(nat[2])
        with open(os.path.join(HERE, "_exit_inject.bin"), "wb") as f:
            f.write(inj[2])
        with open(os.path.join(HERE, "_mid_native.bin"), "wb") as f:
            f.write(nat[1])
        with open(os.path.join(HERE, "_mid_inject.bin"), "wb") as f:
            f.write(inj[1])
        print("\n(落盘: _exit_*.bin / _mid_*.bin)")
    except OSError:
        pass

    for lbl, m in (("native", nat[1]), ("inject", inj[1])):
        print("[{}] mid {}B  含 ?1049h={}  ?1049l={}  ?1049= {}".format(
            lbl, len(m), b"?1049h" in m, b"?1049l" in m, m.count(b"?1049")))

    print("\n=== 屏幕对照 ===")
    for lbl, r in (("native", nat), ("inject", inj)):
        try:
            lines = [x.rstrip() for x in r[3].text().split("\n")[:40] if x.strip()]
        except Exception:
            lines = []
        print("[{}] cursor={} 行={}".format(lbl, r[3].cursor(), len(lines)))
        for ln in lines[:8]:
            print("   {!r}".format(ln[:110]))

    print("\n=== post 字节首处分叉 ===")
    d = first_diff(nat[2], inj[2])
    print("first_diff at byte {}".format(d))
    if d >= 0:
        print("  native[d-20:d+60] = {!r}".format(nat[2][max(0, d - 20):d + 60]))
        print("  inject[d-20:d+60] = {!r}".format(inj[2][max(0, d - 20):d + 60]))


if __name__ == "__main__":
    main()
