# -*- coding: utf-8 -*-
r"""console_mode_freeze_probe.py —— 验证「DLL 吞掉 SetConsoleMode ⇒ 真实控制台输入模式被冻结」

假设（来自 child_input_probe 的实测 + ModeHooks.cpp:10 的 `SetConsoleMode 不调原 API`）：
  父 shell（pwsh/PSReadLine）在"读键"与"执行命令"两种状态间来回切控制台输入模式
  （raw 0x1e4 ⇄ 行模式 0x1f7）。原生运行时这套切换**真的落在控制台上**，
  于是它在执行命令期间 spawn 的子进程看到的是 0x1f7（行模式）→ Python 的 input() 正常。
  注入后 DLL 吞掉所有 SetConsoleMode（不调原 API）⇒ 真实控制台停在注入瞬间的 raw 模式，
  子进程一律看到 0x1e4（行模式关）→ 行式读取挂住、按键不回显。

本探针只量一件事：**父 shell 执行命令期间，目标控制台的真实输入模式是多少**。
  native 组：不注入 → 期望 0x1f7（PSReadLine 已还原）
  inject 组：注入   → 期望 0x1e4（被冻结，不随父 shell 变）

读法：探针进程内**没有 DLL**，用 AttachConsole + CONIN$ 读到的就是真值，
不会被 GetConsoleMode 的 Hook 骗。

    python console_mode_freeze_probe.py
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "e2e"))

from common.paths import PROJECT_ROOT, BUILD_BIN, ti_log_path

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)

PS7_EXE = "".join(["p", "w", "s", "h", ".exe"])
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
SLEEP_CMD = "Start-Sleep -Seconds 6\r"   # shell 自身执行，期间 PSReadLine 不读键


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _drain(pty, seconds, interval=0.1):
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        try:
            d = bytes(pty.read(65536, timeout=interval))
        except Exception:
            break
        if d:
            buf += d
    return buf


def read_console_mode(pid):
    """从本进程 AttachConsole 到 pid 的控制台，读真实输入模式。"""
    k = ctypes.windll.kernel32
    k.FreeConsole()
    att = k.AttachConsole(pid)
    if not att:
        return None, ctypes.get_last_error()
    hc = k.CreateFileW("CONIN$", 0xC0000000, 3, None, 3, 0, None)
    m = ctypes.c_ulong(0)
    ok = k.GetConsoleMode(ctypes.c_void_p(hc), ctypes.byref(m))
    k.FreeConsole()
    return (m.value if ok else None), ctypes.get_last_error()


def _case(inject, pywezterm):
    pty = d_pty = None
    target_pid = 0
    try:
        pty = pywezterm.Pty(120, 30)
        target_pid, _ = pty.spawn([PS7_EXE, "-NoLogo"])
        _drain(pty, 3.5)

        if inject:
            d_pty = pywezterm.Pty(120, 30)
            d_pty.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])
            mlog = ti_log_path(target_pid)
            t0 = time.time()
            while time.time() - t0 < 25 and "Handshake OK" not in _read(mlog):
                time.sleep(0.2)
            _drain(d_pty, 1.0)
            term = d_pty
            label = "注入"
        else:
            term = pty
            label = "不注入"

        # 状态 A：shell 停在提示符（PSReadLine 正在读键）
        mode_idle, _ = read_console_mode(target_pid)
        # 状态 B：shell 正在执行命令（PSReadLine 不读键）
        term.write(list(SLEEP_CMD.encode("utf-8")))
        time.sleep(2.0)
        mode_busy, _ = read_console_mode(target_pid)
        _drain(term, 5.0)

        print("  {:<6} 提示符态=0x{}  执行命令态=0x{}".format(
            label,
            "%x" % mode_idle if mode_idle else "?",
            "%x" % mode_busy if mode_busy else "?"))
        return mode_idle, mode_busy
    finally:
        for p in (d_pty, pty):
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
                psutil.Process(target_pid).terminate()
        except Exception:
            pass


def main():
    try:
        import pywezterm
    except ImportError as e:
        print("无法 import pywezterm: {}".format(e))
        return 1

    print("=" * 78)
    print("父 shell 两个状态下，目标控制台的真实输入模式（0x1e4=行模式关 / 0x1f7=行模式开）")
    print("=" * 78)
    n_idle, n_busy = _case(False, pywezterm)
    i_idle, i_busy = _case(True, pywezterm)
    print("\n" + "=" * 78)
    print("判读：")
    print("  不注入若 提示符态=0x1e4 且 执行命令态=0x1f7 ⇒ 父 shell 的模式切换确实落到控制台")
    print("  注入若 两态都是 0x1e4 ⇒ DLL 吞掉 SetConsoleMode，控制台模式被冻结（根因成立）")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
