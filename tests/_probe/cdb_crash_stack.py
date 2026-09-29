#!/usr/bin/env python3
"""用 cdb 抓"卸载后 3 秒 ≥ 输入 → 目标 shell 崩溃 0xc0000005"的崩溃栈。

已有结论（前几个探针）：
  - 触发条件不是"按 Enter"，而是**卸载完成 3 秒之后**任何一次真实输入
    （settle ≤ 2.9s 全绿，≥ 3.0s 稳定崩，退出码 0xC0000005）
  - 3.0s 这个整数阈值 = `src/dll/state/StatePoller.h: kPollDurationMs = 3000`

但阈值吻合 ≠ 根因。本探针把 cdb 挂到源 shell 上，抓到崩溃现场的调用栈。

做法：不用 PwSession（它用 pty.spawn，拿不到可调试的进程句柄），改为
    1. cdb -o -g -G 起 pwsh（"调试器即父进程"）
    2. 源终端 = pywezterm.Pty 里跑 cdb
    3. mediator 照常注入 cdb 下的 pwsh（target-pid = pwsh 的 pid）
      —— 注意 cdb 是 pwsh 的父进程，pid 用 cdb 报告的 pwsh pid

简化：本探针用 **cdb 直接加载 pwsh**（`cdb -o -g -G pwsh.exe -NoLogo`），
把 cdb 当成"源终端里的 shell"。mediator 的 --target-pid 传 **pwsh 的 pid**
（从 cdb 启动后打印或从进程树取子进程）。

用法：
    python tests/_probe/cdb_crash_stack.py
    python tests/_probe/cdb_crash_stack.py --settle 3.5
"""
import argparse
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
V2 = os.path.normpath(os.path.join(HERE, "..", "e2e_v2"))
sys.path.insert(0, V2)

from pwcommon import paths           # noqa: E402
from pwcommon import pyterm          # noqa: E402
from pwcommon.session import PS7_EXE   # noqa: E402

COLS, ROWS = 120, 30
TUI_SCRIPT = os.path.join(paths.PROJECT_ROOT, "tests", "live", "textual", "taskboard.py")
TUI_TITLE = "TaskBoard"

CDB_CANDIDATES = [
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
    r"C:\Program Files\Windows Kits\10\Debuggers\x64\cdb.exe",
]


def find_cdb() -> str:
    env = os.environ.get("TI_CDB_TOOLS")
    if env:
        for name in ("cdb.exe",):
            p = os.path.join(env, name)
            if os.path.exists(p):
                return p
    for p in CDB_CANDIDATES:
        if os.path.exists(p):
            return p
    raise RuntimeError("找不到 cdb.exe（设 TI_CDB_TOOLS 指向 Debuggers 目录）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", type=float, default=3.5,
                    help="卸载后等待秒数（≥3 秒才会崩）")
    args = ap.parse_args()

    pywezterm = pyterm.require()
    cdb = find_cdb()
    print("cdb = {}".format(cdb))

    missing = paths.preflight()
    if missing:
        print("前提缺失: {}".format("; ".join(missing)))
        return 1

    logdir = os.path.join(paths.BUILD_BIN, "logs")
    os.makedirs(logdir, exist_ok=True)
    cdb_log = os.path.join(logdir, "cdb-target-{}.log".format(int(time.time())))

    # ── 源终端：cdb 调 pwsh（cdb 作为源 shell 的宿主）─────────────────
    src_pty = pywezterm.Pty(COLS, ROWS)
    src_term = pywezterm.Terminal(COLS, ROWS, scrollback=2000)
    cdb_argv = [cdb, "-o", "-g", "-G", "-logo", cdb_log,
                PS7_EXE, "-NoLogo"]
    cdb_pid, _ = src_pty.spawn(cdb_argv, cwd=paths.PROJECT_ROOT)
    print("cdb(源终端) pid = {}".format(cdb_pid))

    # 找 cdb 下的 pwsh 子进程
    import psutil
    shell_pid = None
    deadline = time.time() + 25.0
    while time.time() < deadline:
        try:
            kids = psutil.Process(cdb_pid).children(recursive=False)
            for k in kids:
                if k.name().lower().startswith("pwsh"):
                    shell_pid = k.pid
                    break
        except Exception:
            pass
        if shell_pid:
            break
        time.sleep(0.3)
    if not shell_pid:
        print("找不到 cdb 下的 pwsh 子进程")
        try:
            src_pty.close()
        except Exception:
            pass
        return 1
    print("源 pwsh pid = {}".format(shell_pid))

    # ── 泵线程：读 src → feed，回写应答 ─────────────────────────────
    import threading
    stop = threading.Event()
    lock = threading.RLock()

    def pump():
        while not stop.is_set():
            try:
                chunk = bytes(src_pty.read(65536, timeout=0.05))
            except Exception:
                time.sleep(0.02)
                continue
            if not chunk:
                continue
            with lock:
                try:
                    src_term.feed(chunk)
                    resp = bytes(src_term.drain_written())
                except Exception:
                    resp = b""
            if resp:
                try:
                    src_pty.write(list(resp))
                except Exception:
                    pass

    th = threading.Thread(target=pump, daemon=True)
    th.start()

    def stext():
        with lock:
            return src_term.text()

    # ── dst 通道：mediator ─────────────────────────────────────────
    dst_pty = pywezterm.Pty(COLS, ROWS)
    dst_term = pywezterm.Terminal(COLS, ROWS, scrollback=4000)
    med_log = paths.mediator_log(shell_pid)
    try:
        if os.path.exists(med_log):
            os.remove(med_log)
    except OSError:
        pass
    med_pid, _ = dst_pty.spawn(
        [paths.MEDIATOR_EXE, "--mediator", "--target-pid", str(shell_pid)])
    print("mediator pid = {}".format(med_pid))

    dst_lock = threading.RLock()

    def dstpump():
        while not stop.is_set():
            try:
                chunk = bytes(dst_pty.read(65536, timeout=0.05))
            except Exception:
                time.sleep(0.02)
                continue
            if not chunk:
                continue
            with dst_lock:
                try:
                    dst_term.feed(chunk)
                    resp = bytes(dst_term.drain_written())
                except Exception:
                    resp = b""
            if resp:
                try:
                    dst_pty.write(list(resp))
                except Exception:
                    pass

    dth = threading.Thread(target=dstpump, daemon=True)
    dth.start()

    # 等握手
    ok = False
    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            if "Mediator: Handshake OK" in open(med_log, encoding="utf-8", errors="replace").read():
                ok = True
                break
        except OSError:
            pass
        time.sleep(0.2)
    print("握手 = {}".format(ok))
    if not ok:
        print("mediator 日志尾部:")
        try:
            print(open(med_log, encoding="utf-8", errors="replace").read()[-2000:])
        except OSError:
            pass
        return 1

    # ── 在新终端跑 TUI ────────────────────────────────────────────
    time.sleep(1.5)
    cmd = '& "{}" "{}"\r'.format(sys.executable, TUI_SCRIPT)
    dst_pty.write(list(cmd.encode("utf-8")))

    deadline = time.time() + 45.0
    while time.time() < deadline:
        with dst_lock:
            if TUI_TITLE in dst_term.text():
                break
        time.sleep(0.3)
    with dst_lock:
        seen = TUI_TITLE in dst_term.text()
    print("TUI 渲染 = {}".format(seen))
    if not seen:
        return 1

    # 退出 TUI（Tab 移焦点 → q）
    with dst_lock:
        down = bytes(dst_term.key_down("Tab", 0))
        up = bytes(dst_term.key_up("Tab", 0))
    dst_pty.write(list(down + up))
    time.sleep(0.8)
    dst_pty.write(list(b"q"))
    deadline = time.time() + 20.0
    while time.time() < deadline:
        with dst_lock:
            if TUI_TITLE not in dst_term.text():
                break
        time.sleep(0.3)

    # ── 卸载 ─────────────────────────────────────────────────────
    print("卸载中（关 dst 通道）...")
    try:
        dst_pty.close()
    except Exception:
        pass
    time.sleep(0.2)
    try:
        psutil.Process(med_pid).terminate()
    except Exception:
        pass

    # ── 等 settle，然后输入触发 ────────────────────────────────────
    print("等待 {:.1f}s 后输入触发...".format(args.settle))
    time.sleep(args.settle)

    try:
        p = psutil.Process(shell_pid)
        print("  输入前 pwsh alive = {}".format(p.is_running()))
    except Exception:
        print("  输入前 pwsh 已不在")

    src_pty.write(list(b"echo TRIGGER\r"))
    print("已注入 echo TRIGGER\\r，等崩溃...")

    # 等它崩（cdb 会捕获）
    code = None
    try:
        code = psutil.Process(shell_pid).wait(timeout=30.0)
    except Exception:
        pass
    print("pwsh 退出码 = {}".format(code))
    if code is None:
        print("pwsh 未退出（未复现）")

    # ── 收起 cdb（-o 已捕获，这里让它把栈写完）────────────────────
    time.sleep(2.0)
    try:
        psutil.Process(cdb_pid).terminate()
    except Exception:
        pass
    time.sleep(1.0)
    stop.set()
    time.sleep(0.5)

    # ── 打印 cdb 日志 ────────────────────────────────────────────
    print("\n" + "=" * 76)
    print("cdb 日志：{}".format(cdb_log))
    print("=" * 76)
    try:
        text = open(cdb_log, encoding="utf-8", errors="replace").read()
    except OSError as e:
        print("读不到: {}".format(e))
        return 1

    # 只打关键段：异常行 + 调用栈
    lines = text.splitlines()
    keys = ("Access violation", "EXCEPTION", "Stack Trace", "ChildEBP", "RetAddr",
            "***", "0x", "Unloaded_injected", "injected!", "# ")
    shown = 0
    for i, line in enumerate(lines):
        if any(k in line for k in keys):
            print(line[:200])
            shown += 1
            if shown > 200:
                break
    if shown == 0:
        print("（日志里没找到异常关键字，全文尾部 80 行）")
        for line in lines[-80:]:
            print(line[:200])

    print("\n完整日志：{}".format(cdb_log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
