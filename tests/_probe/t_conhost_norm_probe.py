# -*- coding: utf-8 -*-
"""t_conhost_norm_probe.py —— 探测 ConHost/ConPTY 对 VT 序列的"规范化"清单

目的
----
劫持后要让 WT 收到的流 == ConHost 处理后的流。先要搞清：ConHost 到底对
哪些序列做了"消化/改写/丢弃"。

方法
----
对每条候选序列，单独用真 ConPTY（pywezterm.Pty，无注入）跑一个小进程：
  程序写：<前导标记> + <候选序列> + <后置标记>，然后退出。
ConPTY 回吐的字节 = ConHost 处理后的规范流。
比对：候选序列是否原样出现在回吐里 / 是否被改写 / 是否消失。

把候选按类分组，每组独立一进程，避免互相干扰。

    python t_conhost_norm_probe.py
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

SEP = "=" * 78

_HELPER = r"""
import ctypes, os, sys
k = ctypes.windll.kernel32
h = k.GetStdHandle(-11)
m = ctypes.c_ulong(0)
k.GetConsoleMode(h, ctypes.byref(m))
k.SetConsoleMode(h, m.value | 0x0001 | 0x0004)
os.write(1, bytes.fromhex(sys.argv[1]))
"""


def conpty_roundtrip(payload: bytes, timeout=3.0) -> bytes:
    p = pywezterm.Pty(120, 40)
    p.spawn([sys.executable, "-c", _HELPER, payload.hex()])
    out = b""
    dl = time.time() + timeout
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


def hexs(b):
    return " ".join("{:02X}".format(x) for x in b)


def fmt(b):
    """把字节转成可读形式（转义可见）。"""
    out = []
    for x in b:
        if x == 0x1B:
            out.append("ESC")
        elif x == 0x0D:
            out.append("\\r")
        elif x == 0x0A:
            out.append("\\n")
        elif 0x20 <= x < 0x7F:
            out.append(chr(x))
        else:
            out.append("<%02X>" % x)
    return "".join(out)


# 候选序列分组：每组一个独立进程
# 每条: (标签, 序列字节)
GROUPS = [
    ("A 换行/回车", [
        ("bare LF (\\n)",            b"\x1b[2;1HAAA\nBBB\x1b[10;1H"),
        ("CR alone (\\r)",           b"\x1b[2;1HAAA\x1b[10;1H" + b"\rCCC"),
        ("CRLF (\\r\\n)",            b"\x1b[2;1HAAA\r\nBBB\x1b[10;1H"),
        ("CR + CSI2K inside line",   b"\x1b[2;1HAAA\r\x1b[2K\x1b[10;1H"),
    ]),
    ("B 擦行 EL (CSI nK)", [
        ("CSI 0K",                   b"\x1b[2;1HABCDEF\x1b[2;3H\x1b[0K\x1b[10;1H"),
        ("CSI 1K",                   b"\x1b[2;1HABCDEF\x1b[2;3H\x1b[1K\x1b[10;1H"),
        ("CSI 2K",                   b"\x1b[2;1HABCDEF\x1b[2;1H\x1b[2K\x1b[10;1H"),
    ]),
    ("C 擦屏 ED (CSI nJ)", [
        ("CSI 0J",                   b"\x1b[2;1HABCDEF\x1b[3;1H\x1b[0J\x1b[10;1H"),
        ("CSI 1J",                   b"\x1b[2;1HABCDEF\x1b[3;1H\x1b[1J\x1b[10;1H"),
        ("CSI 2J",                   b"\x1b[2;1HABCDEF\x1b[2J\x1b[10;1H"),
    ]),
    ("D 光标移动", [
        ("CUP",                      b"\x1b[2;1HAAA\x1b[5;10HBBB\x1b[10;1H"),
        ("CUU/CUD (A/B)",            b"\x1b[2;1HAAA\x1b[2A\x1b[2BBBB\x1b[10;1H"),
        ("CHA (G) 到列",             b"\x1b[2;1HAAA\x1b[2;5G\x1b[10;1H"),
        ("CUP 0,0 = H",              b"\x1b[2;1HAAA\x1b[H\x1b[10;1H"),
    ]),
    ("E 其他常见", [
        ("BS 退格 0x08",             b"\x1b[2;1HABCDEF\x08\x08\x08\x1b[10;1H"),
        ("HT 制表 0x09",             b"\x1b[2;1HAAA\tBBB\x1b[10;1H"),
        ("BEL 0x07",                 b"\x1b[2;1HAAA\x07BBB\x1b[10;1H"),
        ("CSI 3J (擦回滚)",           b"\x1b[2;1HAAA\x1b[3J\x1b[10;1H"),
        ("DECSC/DECRC (ESC7/8)",     b"\x1b[2;5H\x1b7\x1b[5;1H\x1b8XXX\x1b[10;1H"),
    ]),
]

TAIL = b"\x1b[20;1HSENTINEL\n"


def main():
    print(SEP)
    print("ConHost/ConPTY VT 序列规范化探测（无注入，纯 ConPTY 往返）")
    print("判据：候选序列若在回吐中原样出现 → 透传；消失/改写 → 被消化/规范化")
    print(SEP)

    for gname, cases in GROUPS:
        print("\n### {}".format(gname))
        for label, seq in cases:
            payload = seq + TAIL
            out = conpty_roundtrip(payload)
            present = seq in out
            # 也检查去掉末尾定位后是否出现（部分序列可能被合并）
            print("\n  -- {} --".format(label))
            print("     发送: {}".format(fmt(seq)))
            print("     回吐: {}".format(fmt(out)[:200]))
            print("     原样保留 = {}".format(present))

    print("\n" + SEP)
    print("DONE")
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
