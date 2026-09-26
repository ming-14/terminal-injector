# -*- coding: utf-8 -*-
r"""t_shell_child_exit.py —— 【通用】注入 shell 后跑任意子程序，Ctrl+C 退出后的画面/光标对照

要解决的通用问题
----------------
用户报告：WT 里注入 shell → 运行某全屏 TUI（opentui-examples.exe）→ Ctrl+C
         → 命令行文本被错位覆盖 / 光标异常。原生 WT 无此现象。

不该给单个程序做特例。本探针做成**通用**的：
  目标 shell（pwsh / cmd 可切）+ 目标子程序（任意，参数化）
  双轨对照（原生 ConPTY vs 注入），Ctrl+C 后比对：
    - 屏幕非空行集合
    - 光标坐标
    - 关键字节（新 prompt 前是否有 CR/LF、有无 ?1049l、有无重复 prompt 串）

用法
----
    set TI_TUI_TARGET=C:\path\to\tui.exe            # 目标全屏 TUI（必填）
    python tests/_probe/t_shell_child_exit.py       # pwsh + $TI_TUI_TARGET
    python tests/_probe/t_shell_child_exit.py --shell cmd
    python tests/_probe/t_shell_child_exit.py --child "C:\...\python.exe" --args -c --args "print(1)"
    python tests/_probe/t_shell_child_exit.py --only native

判据
----
两轨「Ctrl+C 退出后的屏幕」应一致；不一致的行即劫持链路引入的偏差（= bug 所在）。
"""
import argparse
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

MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
CMD_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "cmd.exe")
PS7_EXE = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                       "PowerShell", "7", "".join(["p", "w", "s", "h", ".exe"]))
# 目标全屏 TUI 由环境变量指定（无默认值，避免写死本机路径）
DEFAULT_CHILD = os.environ.get("TI_TUI_TARGET", "")

COLS, ROWS = 120, 30
CTRL_C = b"\x03"


# ------------------------------------------------------------- 基础

def pump(p, t, sec, acc=None):
    """读 pty → feed 终端 → 回写应答。**带外层 deadline，绝不死等**（这是之前
    探针 hang 的根因：只靠 read 的 timeout，循环条件写错就死循环）。"""
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
    if acc is not None:
        acc.append(buf)
    return buf


def nonempty(t, n=40, maxw=110):
    try:
        lines = t.text().split("\n")
    except Exception:
        return []
    return [x.rstrip()[:maxw] for x in lines[:n] if x.strip()]


def dump(t, label, rows=12):
    print("\n  [{}]".format(label))
    for i, ln in enumerate(nonempty(t, rows)):
        print("    {:2d}|{}".format(i + 1, ln))
    try:
        print("    → cursor = {}".format(t.cursor()))
    except Exception:
        pass


def wait_handshake(mlog, timeout=25.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with open(mlog, "r", encoding="utf-8", errors="ignore") as f:
                if "Handshake OK" in f.read():
                    return True, time.time() - t0
        except OSError:
            pass
        time.sleep(0.2)
    return False, time.time() - t0


# ------------------------------------------------------------- 轨道 A 原生

def run_native(pywezterm, shell_argv, child_cmd):
    print("\n" + "=" * 78)
    print("轨道 A 原生：{} 里跑子程序（不注入）".format(os.path.basename(shell_argv[0])))
    print("=" * 78)
    p = pywezterm.Pty(COLS, ROWS)
    t = pywezterm.Terminal(COLS, ROWS, scrollback=5000)
    pid, _ = p.spawn(shell_argv)
    print("  shell pid = {}".format(pid))
    boot = pump(p, t, 3.5)
    print("  [boot] {} 字节, 首行={!r}".format(
        len(boot), (nonempty(t, 1) or [""])[0]))

    p.write(list(child_cmd.encode("utf-8")))
    mid = pump(p, t, 10.0)
    print("  [run] {} 字节".format(len(mid)))

    p.write(list(CTRL_C))
    post = pump(p, t, 6.0)
    dump(t, "Ctrl+C 退出后")
    print("  [exit] {} 字节 | 末 200B = {!r}".format(len(post), bytes(post[-200:])))
    out = {"boot": boot, "mid": mid, "post": post,
           "screen": nonempty(t), "cursor": _cur(t)}
    try:
        p.close()
    except Exception:
        pass
    return out


# ------------------------------------------------------------- 轨道 B 注入

def run_inject(pywezterm, shell_argv, child_cmd, log_keys):
    print("\n" + "=" * 78)
    print("轨道 B 注入：先注入 {}，再在目标终端里跑子程序".format(
        os.path.basename(shell_argv[0])))
    print("=" * 78)
    t_pty = d_pty = None
    target_pid = 0
    try:
        # 目标 shell 跑在源 ConPTY
        t_pty = pywezterm.Pty(COLS, ROWS)
        target_pid, _ = t_pty.spawn(shell_argv)
        print("  [setup] 目标 shell pid = {}".format(target_pid))
        pump(t_pty, pywezterm.Terminal(COLS, ROWS), 3.5)

        # 目标终端：mediator 跑在另一个 ConPTY（冒充 WT）
        d_pty = pywezterm.Pty(COLS, ROWS)
        t = pywezterm.Terminal(COLS, ROWS, scrollback=5000)
        med_pid, _ = d_pty.spawn([MEDIATOR_EXE, "--mediator",
                                  "--target-pid", str(target_pid)])
        ok, dt = wait_handshake(ti_log_path(target_pid))
        print("  [setup] mediator pid={} 握手 {} ({:.1f}s)".format(
            med_pid, "OK" if ok else "超时", dt))
        boot = pump(d_pty, t, 2.0)
        print("  [boot] 目标终端 {} 字节".format(len(boot)))
        dump(t, "注入后（子程序运行前）", rows=6)

        # 在目标终端里跑子程序（走完整注入链路，含子进程注入）
        d_pty.write(list(child_cmd.encode("utf-8")))
        mid = pump(d_pty, t, 10.0)
        print("  [run] {} 字节".format(len(mid)))

        d_pty.write(list(CTRL_C))
        post = pump(d_pty, t, 6.0)
        dump(t, "Ctrl+C 退出后")
        print("  [exit] {} 字节 | 末 200B = {!r}".format(
            len(post), bytes(post[-200:])))

        print("\n  [日志] 关键行（{}）:".format("/".join(log_keys)))
        try:
            with open(childlog.latest_injected_log(target_pid), "r",
                      encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            hits = [x for x in txt.splitlines() if any(k in x for k in log_keys)]
            for ln in hits[-25:]:
                i = ln.find("]")
                print("    " + (ln[i + 1:] if i >= 0 else ln).strip()[:160])
            if not hits:
                print("    （无匹配）")
        except OSError as e:
            print("    （读日志失败 {}）".format(e))

        return {"boot": boot, "mid": mid, "post": post,
                "screen": nonempty(t), "cursor": _cur(t)}
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


def _cur(t):
    try:
        return t.cursor()
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shell", choices=["pwsh", "cmd"], default="pwsh")
    ap.add_argument("--child", default=DEFAULT_CHILD)
    ap.add_argument("--args", action="append", default=[])
    ap.add_argument("--only", choices=["native", "inject"], default=None)
    a = ap.parse_args()

    try:
        import pywezterm
    except ImportError as e:
        print("无法 import pywezterm ({}): {}".format(PWTERM_DIR, e))
        return 1

    shell_argv = [PS7_EXE, "-NoLogo"] if a.shell == "pwsh" else [CMD_EXE]
    for exe, nm in ((shell_argv[0], a.shell), (a.child, "child")):
        if not os.path.exists(exe):
            print("[SKIP] {} 不存在: {}".format(nm, exe))
            return 2

    # 子程序命令行：引号包住路径
    if a.shell == "pwsh":
        child_cmd = '& "{0}"{1}\r'.format(
            a.child, "".join(' ' + x for x in a.args))
    else:
        child_cmd = '"{0}"{1}\r'.format(
            a.child, "".join(' ' + x for x in a.args))

    print("pywezterm =", pywezterm.version())
    print("shell = {} | child = {}".format(a.shell, a.child))
    print("child_cmd = {!r}".format(child_cmd))

    keys = ["LazyInit", "prompt", "Prompt", "replay", "Replay",
            "SetConsoleCursorPosition_Detour", "cursor"]
    res = {}
    if a.only != "inject":
        res["native"] = run_native(pywezterm, shell_argv, child_cmd)
    if a.only != "native":
        res["inject"] = run_inject(pywezterm, shell_argv, child_cmd, keys)

    print("\n" + "=" * 78)
    print("对照结果")
    print("=" * 78)
    for k, v in res.items():
        print("\n  [{}] 光标={} 非空行 {} 行:".format(k, v["cursor"], len(v["screen"])))
        for ln in v["screen"][:8]:
            print("      {!r}".format(ln))
    if len(res) == 2:
        na, nb = res["native"]["screen"], res["inject"]["screen"]
        print("\n  屏幕一致: {}".format("PASS" if na == nb else "FAIL"))
        if na != nb:
            print("    仅原生有: {}".format([x for x in na if x not in nb]))
            print("    仅注入有: {}".format([x for x in nb if x not in na]))
        print("  光标一致: {} ({} vs {})".format(
            "PASS" if res["native"]["cursor"] == res["inject"]["cursor"] else "FAIL",
            res["native"]["cursor"], res["inject"]["cursor"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
