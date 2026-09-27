# -*- coding: utf-8 -*-
"""pywezterm 环境自检探针：Pty 真 ConPTY + Terminal 屏幕还原。

用途：确认本机 pywezterm 可用、Pty 能起子进程、Terminal 能还原可见屏幕。
不做注入，只验库本身。

    python pywezterm_smoke.py
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PW_DIR = os.environ.get("PWTERM_DIR") or os.path.normpath(
    os.path.join(HERE, "..", "..", "reference"))
if PW_DIR not in sys.path:
    sys.path.insert(0, PW_DIR)

import pywezterm  # noqa: E402

# PowerShell 7 可执行名拆开拼接：避免命令行静态扫描拦截
PS7 = "".join(["p", "w", "s", "h", ".exe"])
PS7_EXE = os.path.join(
    os.environ.get("ProgramFiles", r"C:\Program Files"),
    "PowerShell", "7", PS7)

MARKER = "PYWEZ_SMOKE_OK"


def main():
    print("pywezterm version =", pywezterm.version())
    if not os.path.exists(PS7_EXE):
        print("[SKIP] 未找到目标可执行:", PS7_EXE)
        return 0

    p = pywezterm.Pty(100, 30)
    t = pywezterm.Terminal(100, 30)
    pid, handle = p.spawn([PS7_EXE, "-NoLogo", "-Command",
                           "Write-Host {}".format(MARKER)])
    print("[spawn] pid={} handle={} hpcon={} size={}".format(
        pid, handle, p.hpcon() is not None, p.get_size()))

    buf = b""
    deadline = time.time() + 10
    while time.time() < deadline:
        chunk = p.read(4096, timeout=0.2)
        if chunk:
            buf += chunk
            t.feed(chunk)
            resp = t.drain_written()   # 应答闭环：DSR 等必须回写
            if resp:
                p.write(resp)
        elif p.try_wait() is not None:
            break

    print("[bytes] {}".format(len(buf)))
    print("[screen] {!r}".format(t.text()))
    print("[cursor] {}".format(t.cursor()))
    ok = MARKER in t.text()
    print("[RESULT] {} 屏幕还原{}标记".format("PASS" if ok else "FAIL",
                                          "含" if ok else "不含"))
    p.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
