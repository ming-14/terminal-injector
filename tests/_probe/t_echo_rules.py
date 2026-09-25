# -*- coding: utf-8 -*-
"""t_echo_rules.py —— 精确测定 ConHost 在 LINE_INPUT+ECHO_INPUT 下对按键的回显字节。

方法：真 ConPTY。子进程保持默认输入模式(0x1f7: LINE+ECHO)，用
     SetConsoleMode 确认，然后逐个喂按键，观察 ConHost 回吐的输出字节。

判据：每个按键 → ConHost 回显的字节序列。
    python t_echo_rules.py
"""
import os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "reference"))
import pywezterm

_HELPER = r'''
import ctypes, sys, os, time
k = ctypes.windll.kernel32
k.SetConsoleOutputCP(65001); k.SetConsoleCP(65001)
hout = k.GetStdHandle(-11)
m = ctypes.c_ulong(0); k.GetConsoleMode(hout, ctypes.byref(m))
k.SetConsoleMode(hout, m.value | 0x0001 | 0x0004)   # PROCESSED_OUTPUT|VT_PROCESSING
hin = k.GetStdHandle(-10)
mi = ctypes.c_ulong(0); k.GetConsoleMode(hin, ctypes.byref(mi))
sys.stderr.write("INPUT_MODE=0x%x\n" % mi.value); sys.stderr.flush()
# 标记起点
os.write(1, b"<<MARK>>")
# 直接调 ReadConsoleA 读行(会阻塞到 Enter)。回显由 ConHost 负责。
buf = ctypes.create_string_buffer(256)
rd = ctypes.c_ulong(0)
ok = k.ReadConsoleA(hin, buf, 255, ctypes.byref(rd), None)
os.write(1, b"<<RET ok=%d rd=%d>>" % (ok, rd.value))
'''


def capture(keys, wait=3.0):
    p = pywezterm.Pty(100, 30)
    p.spawn([sys.executable, "-c", _HELPER])
    buf = b""
    t0 = time.time()
    sent_idx = 0
    last = time.time()
    while time.time() - last < wait:
        c = p.read(8192, 0.2)
        if c:
            buf += c
            last = time.time()
        # MARK 出现后开始按键
        if sent_idx < len(keys) and b"<<MARK>>" in buf:
            # 逐个按键，间隔 0.3s
            if time.time() - t0 > 0.5 + sent_idx * 0.3:
                try:
                    p.write(keys[sent_idx])
                    sent_idx += 1
                except Exception:
                    pass
        elif p.try_wait() is not None and b"<<RET" in buf:
            break
    p.close()
    return buf


def fmt(x):
    o = []
    for ch in x:
        if ch == 0x1b: o.append("ESC")
        elif ch == 0x0d: o.append("\r")
        elif ch == 0x0a: o.append("\n")
        elif ch == 0x08: o.append("\b")
        elif ch == 0x07: o.append("\a")
        elif 0x20 <= ch < 0x7f: o.append(chr(ch))
        else: o.append("<%02X>" % ch)
    return "".join(o)


CASES = [
    ("可打印 ab_1",  [b"a", b"b", b"1"]),
    ("Enter",        [b"\r"]),
    ("Backspace",    [b"a", b"b", b"\x08", b"\r"]),
    ("Tab",          [b"a", b"\t", b"\r"]),
    ("Esc",          [b"a", b"\x1b", b"\r"]),
    ("Ctrl+C",       [b"a", b"\x03"]),
    ("方向键UP",     [b"\x1b[A", b"\r"]),
    ("组合 abc\bX\r", [b"a", b"b", b"c", b"\x08", b"X", b"\r"]),
]

print("=" * 74)
print("ConHost LINE_INPUT+ECHO_INPUT 回显规则探测")
print("=" * 74)
for name, keys in CASES:
    b = capture(keys)
    print("\n[%s] keys=%r" % (name, keys))
    print("  回吐: %s" % fmt(b))
print("\n" + "=" * 74)
