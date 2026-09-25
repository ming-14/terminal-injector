# -*- coding: utf-8 -*-
"""t_conhost_newline_probe.py —— 精确测定 ConHost 对 LF / CR / CRLF 的规范化

目的：为"直通流 == ConHost 处理后流"确定 LF 规范化规则。
方法：真 ConPTY 往返，逐条比对 ConHost 回吐，并用 pywezterm.Terminal 还原屏幕。

判据（预期 ConHost 语义）：
  - 裸 LF (\\n)      -> 应等价 CR+LF（回到列 0 并下移一行）
  - 裸 CR (\\r)      -> 回到列 0，不下移
  - CRLF (\\r\\n)     -> 同裸 LF（不得变成 \\r\\r\\n 双下移）

    python t_conhost_newline_probe.py
"""
import os
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
    os.path.join(HERE, "..", ".."))
PW_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PW_DIR not in sys.path:
    sys.path.insert(0, PW_DIR)
import pywezterm  # noqa: E402

SEP = "=" * 74

_HELPER = r"""
import ctypes, os, sys
k = ctypes.windll.kernel32
k.SetConsoleOutputCP(65001)
k.SetConsoleCP(65001)
h = k.GetStdHandle(-11)
m = ctypes.c_ulong(0)
k.GetConsoleMode(h, ctypes.byref(m))
k.SetConsoleMode(h, m.value | 0x0001 | 0x0004)
os.write(1, bytes.fromhex(sys.argv[1]))
"""


def rt(payload, t=2.5):
    p = pywezterm.Pty(80, 24)
    p.spawn([sys.executable, "-c", _HELPER, payload.hex()])
    out = b""
    dl = time.time() + t
    while time.time() < dl:
        c = p.read(8192, 0.2)
        if c:
            out += c
        elif p.try_wait() is not None:
            while True:
                c = p.read(8192, 0.3)
                if not c:
                    break
                out += c
            break
    p.close()
    return out


def fmt(b):
    o = []
    for x in b:
        if x == 0x1B:
            o.append("ESC")
        elif x == 0x0D:
            o.append("\\r")
        elif x == 0x0A:
            o.append("\\n")
        elif 0x20 <= x < 0x7F:
            o.append(chr(x))
        else:
            o.append("<%02X>" % x)
    return "".join(o)


def screen_of(data):
    t = pywezterm.Terminal(80, 24)
    t.feed(data)
    return t.text().rstrip("\n").split("\n")


CASES = [
    ("LF only  AAA\\nBBB", b"AAA\nBBB"),
    ("CR only  AAA\\rBBB", b"AAA\rBBB"),
    ("CRLF     AAA\\r\\nBBB", b"AAA\r\nBBB"),
    ("CR CR LF AAA\\r\\r\\nBBB", b"AAA\r\r\nBBB"),
    ("LF twice AAA\\n\\nBBB", b"AAA\n\nBBB"),
]


def main():
    print(SEP)
    print("ConHost 对 LF / CR / CRLF 的规范化实测")
    print(SEP)
    for name, seq in CASES:
        out = rt(seq)
        print("\n-- {} --".format(name))
        print("   发送   : {}".format(fmt(seq)))
        print("   回吐   : {}".format(fmt(out)[:120]))
        # ConHost 回吐的规范化之后，pywezterm 解析的屏幕行
        sc = screen_of(out)
        print("   屏幕(ConHost语义):")
        for i, ln in enumerate(sc):
            if ln.strip():
                print("      {:>2}| col={:<2} {!r}".format(
                    i, len(ln) - len(ln.lstrip()), ln[:60]))
        # 若把原序列直接喂（= 注入链路语义），屏幕如何
        sc2 = screen_of(seq)
        print("   屏幕(原样直通)  :")
        for i, ln in enumerate(sc2):
            if ln.strip():
                print("      {:>2}| col={:<2} {!r}".format(
                    i, len(ln) - len(ln.lstrip()), ln[:60]))

    print("\n" + SEP)
    print("DONE")
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
