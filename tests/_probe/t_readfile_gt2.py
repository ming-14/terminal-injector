# -*- coding: utf-8 -*-
"""ConHost 真值: ReadFile(stdin, 256) 在 LINE_INPUT+ECHO 下分几次返回。"""
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
k.SetConsoleMode(hout, m.value | 0x0001 | 0x0004)
os.write(1, b"<<MARK>>")
for i in range(3):
    b = os.read(0, 256)
    os.write(1, b"<<R%d len=%d %r>>" % (i, len(b), b))
os.write(1, b"<<END>>")
'''

p = pywezterm.Pty(100, 30)
p.spawn([sys.executable, "-c", _HELPER])
buf = b""; t0 = time.time(); sent = False; last = time.time()
while time.time() - last < 5.0:
    c = p.read(8192, 0.2)
    if c:
        buf += c; last = time.time()
    if not sent and b"<<MARK>>" in buf and time.time() - t0 > 0.6:
        p.write(b"ab\r"); sent = True
    if b"<<END>>" in buf:
        break
p.close()
def fmt(x):
    o=[]
    for ch in x:
        if ch==0x1b: o.append("ESC")
        elif ch==0x0d: o.append("\r")
        elif ch==0x0a: o.append("\n")
        elif ch==0x08: o.append("\b")
        elif 0x20<=ch<0x7f: o.append(chr(ch))
        else: o.append("<%02X>"%ch)
    return "".join(o)
i = buf.find(b"<<MARK>>")
print("read(256) 回吐:", fmt(buf[i:]))
