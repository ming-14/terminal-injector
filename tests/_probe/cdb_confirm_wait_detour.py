#!/usr/bin/env python3
"""最终验证：证明崩溃线程是"The 一个阻塞在 WaitForMultipleObjectsEx_Detour 里的
线程，在 DLL 被远程 FreeLibrary 之后从 call orig 返回，执行到 detour 尾部（+0x3011b）
那条指令时踩空"。

前一个探针（cdb_crash_stack.py）已经抓到：
    injected.dll 加载基址 = 0x7ff8`b5a60000
    崩溃地址             = 0x7ff8`b5a9011b   -> rva 0x3011b
    rva 0x3011b 反汇编   = WaitForMultipleObjectsEx_Detour+0x170 的 `mov edi,eax`
                           （即 `call r10`(=orig) 的返回点）
    崩溃处内存字节 = 00 41 52（垃圾）  vs 磁盘字节 = 8b f8（mov edi,eax）

本探针不再依赖 cdb 的符号解析（它把地址错认成 Microsoft.PowerShell.*），
改为在 cdb 里**显式对 `injected.dll + 0x3011b` 下断**，并在命中时打印
**全部线程的调用栈**，直接看是哪个线程、栈上是不是 WaitForMultipleObjectsEx_Detour。

做法：
    sxe av            # 访问违例第一次机会就断
    bp <dllbase>+0x3011b   # 断在 detour 返回点
    ... 命中后 ~*kb + .ecxr + k

由于 dll 基址 ASLR 每次不同，用 cdb 脚本 `.foreach` 或先 `lm` 拿基址。
简化：用 `sxe av` 就够了 —— AV 时栈上就是原始现场。

用法：
    python tests/_probe/cdb_confirm_wait_detour.py
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
from pwcommon.session import PS7_EXE   # noqa: E402

COLS, ROWS = 120, 30
TUI_SCRIPT = os.path.join(paths.PROJECT_ROOT, "tests", "live", "textual", "taskboard.py")
TUI_TITLE = "TaskBoard"

CDB_CANDIDATES = [
    r"C:\Program Files (x86)\Windows Kits\10\Debuggers\x64\cdb.exe",
    r"C:\Program Files\Windows Kits\10\Debuggers\x64\cdb.exe",
]

# cdb 初始命令：AV 第一次机会即断；命中后打印所有线程栈 + 异常上下文
CDB_INIT = (
    ".symfix; .reload; "
    "sxe av; "                       # 访问违例 first-chance 就中断
    "g; "                            # 跑起来
    ".echo ===== AV HIT =====; "
    "r; "                            # 寄存器
    ".ecxr; "                        # 切到异常上下文
    "kb; "                           # 当前线程栈
    ".echo ===== ALL THREADS =====; "
    "~*kb; "                         # 所有线程栈
    ".echo ===== DLL BASE =====; "
    "lm m injected; "
    ".echo ===== FAULTING BYTES =====; "
    "u @rip-10 L20; "
    ".echo ===== END =====; "
    "q"
)


def find_cdb() -> str:
    env = os.environ.get("TI_CDB_TOOLS")
    if env:
        p = os.path.join(env, "cdb.exe")
        if os.path.exists(p):
            return p
    for p in CDB_CANDIDATES:
        if os.path.exists(p):
            return p
    raise RuntimeError("找不到 cdb.exe")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle", type=float, default=3.5)
    args = ap.parse_args()

    import threading
    import psutil

    pywezterm = pyterm.require()
    cdb = find_cdb()

    logdir = os.path.join(paths.BUILD_BIN, "logs")
    os.makedirs(logdir, exist_ok=True)
    cdb_log = os.path.join(logdir, "cdb-av-{}.log".format(int(time.time())))
    print("cdb = {}".format(cdb))
    print("cdb 日志 = {}".format(cdb_log))

    # ── 源终端：pywezterm.Pty 里跑 cdb → cdb 再起 pwsh ───────────────
    src_pty = pywezterm.Pty(COLS, ROWS)
    src_term = pywezterm.Terminal(COLS, ROWS, scrollback=2000)
    cdb_argv = [cdb, "-o", "-g", "-G", "-logo", cdb_log,
                "-c", CDB_INIT, PS7_EXE, "-NoLogo"]
    cdb_pid, _ = src_pty.spawn(cdb_argv, cwd=paths.PROJECT_ROOT)
    print("cdb pid = {}".format(cdb_pid))

    shell_pid = None
    deadline = time.time() + 30.0
    while time.time() < deadline:
        try:
            for k in psutil.Process(cdb_pid).children(recursive=False):
                if k.name().lower().startswith("pwsh"):
                    shell_pid = k.pid
                    break
        except Exception:
            pass
        if shell_pid:
            break
        time.sleep(0.3)
    if not shell_pid:
        print("找不到 pwsh 子进程")
        return 1
    print("pwsh pid = {}".format(shell_pid))

    stop = threading.Event()
    lock = threading.RLock()

    def pump(pty, term, lk):
        while not stop.is_set():
            try:
                chunk = bytes(pty.read(65536, timeout=0.05))
            except Exception:
                time.sleep(0.02)
                continue
            if not chunk:
                continue
            with lk:
                try:
                    term.feed(chunk)
                    resp = bytes(term.drain_written())
                except Exception:
                    resp = b""
            if resp:
                try:
                    pty.write(list(resp))
                except Exception:
                    pass

    threading.Thread(target=pump, args=(src_pty, src_term, lock), daemon=True).start()

    # ── dst：mediator ─────────────────────────────────────────────
    dst_pty = pywezterm.Pty(COLS, ROWS)
    dst_term = pywezterm.Terminal(COLS, ROWS, scrollback=4000)
    dst_lock = threading.RLock()
    med_log = paths.mediator_log(shell_pid)
    try:
        if os.path.exists(med_log):
            os.remove(med_log)
    except OSError:
        pass
    med_pid, _ = dst_pty.spawn(
        [paths.MEDIATOR_EXE, "--mediator", "--target-pid", str(shell_pid)])
    threading.Thread(target=pump, args=(dst_pty, dst_term, dst_lock), daemon=True).start()

    def logtext(path):
        try:
            return open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            return ""

    ok = False
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if "Mediator: Handshake OK" in logtext(med_log):
            ok = True
            break
        time.sleep(0.2)
    print("握手 = {}".format(ok))
    if not ok:
        print(logtext(med_log)[-1500:])
        return 1

    # ── 新终端跑 TUI ──────────────────────────────────────────────
    time.sleep(1.5)
    dst_pty.write(list('& "{}" "{}"\r'.format(sys.executable, TUI_SCRIPT).encode()))
    deadline = time.time() + 45.0
    while time.time() < deadline:
        with dst_lock:
            if TUI_TITLE in dst_term.text():
                break
        time.sleep(0.3)
    with dst_lock:
        if TUI_TITLE not in dst_term.text():
            print("TUI 未渲染，放弃")
            return 1
    print("TUI 已渲染")

    # 退出 TUI（Tab → q）
    with dst_lock:
        dst_pty.write(list(bytes(dst_term.key_down("Tab", 0)) + bytes(dst_term.key_up("Tab", 0))))
    time.sleep(0.8)
    dst_pty.write(list(b"q"))
    deadline = time.time() + 20.0
    while time.time() < deadline:
        with dst_lock:
            if TUI_TITLE not in dst_term.text():
                break
        time.sleep(0.3)
    print("TUI 已退出")

    # ── 卸载 ─────────────────────────────────────────────────────
    print("卸载...")
    try:
        dst_pty.close()
    except Exception:
        pass
    time.sleep(0.2)
    try:
        psutil.Process(med_pid).terminate()
    except Exception:
        pass

    print("等 {:.1f}s 后触发输入".format(args.settle))
    time.sleep(args.settle)
    src_pty.write(list(b"echo TRIGGER\r"))
    print("已注入输入，等 cdb 抓 AV...")

    # cdb 断到 AV 会执行 CDB_INIT 的后半段并 q 退出；等它写完
    deadline = time.time() + 90.0
    while time.time() < deadline:
        t = logtext(cdb_log)
        if "===== END =====" in t:
            break
        try:
            if not psutil.Process(cdb_pid).is_running():
                break
        except Exception:
            break
        time.sleep(0.5)

    stop.set()
    time.sleep(0.5)
    try:
        psutil.Process(cdb_pid).terminate()
    except Exception:
        pass

    print("\n" + "=" * 76)
    print("cdb 日志：{}".format(cdb_log))
    print("=" * 76)
    text = logtext(cdb_log)
    # 打印 AV 段
    idx = text.find("===== AV HIT =====")
    if idx < 0:
        print("未捕获 AV。日志尾部:")
        print(text[-3000:])
        return 1
    end = text.find("===== END =====")
    seg = text[idx:end if end > 0 else idx + 12000]
    print(seg)
    print("\n完整日志：{}".format(cdb_log))
    return 0


if __name__ == "__main__":
    sys.exit(main())
