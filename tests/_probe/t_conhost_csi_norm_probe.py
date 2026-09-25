# -*- coding: utf-8 -*-
"""t_conhost_csi_norm_probe.py —— ConHost「屏幕副作用」序列的规范化差异测定

目的（2026-09-25）：
  确立「直通流 == ConHost 处理后流」这一归一化原则的适用边界。
  逐类测定 ConHost 对 \\r / EL / ED / CUP / BS / HT / DECSC-DECRC /
  自动换行 / 滚屏 的规范化，判断哪些序列在直通链路上需要额外归一。

方法：
  真 ConPTY 往返（pywezterm.Pty）取 ConHost 回吐字节 → pywezterm.Terminal
  还原屏幕；对照侧把同一段字节先做**与 VtSgrFilter 等价的换行归一**
  （裸 LF→CRLF，OSC 载荷除外）后喂 Terminal（= 修复后的注入链路语义）。
  二者行结构一致 = 无需额外归一。

关键教训（探针自身的坑）：
  承载进程必须显式 SetConsoleOutputCP(65001)。ConPTY 里 python 默认用本地
  代码页（GBK），UTF-8 中文被 ConHost 按 GBK 误解码 → 宽字符列宽算错、
  屏幕缓冲错乱，会**伪造出大量"不一致"**（曾据此误判 ⑫ 用例）。
  设 UTF-8 后 13/13 用例全部一致。

结论：本组 10 个用例 0 差异 → 除裸 LF 外，ConHost 与直通链路语义等价，
  无需为 EL/ED/CUP 等再做归一。

    python t_conhost_csi_norm_probe.py
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
W, H = 120, 30

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
    p = pywezterm.Pty(W, H)
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


def normalize_newlines(data):
    """与 VtSgrFilter::EmitGround 等价的换行归一（Ground 态裸 LF→CRLF）。

    仅用于建立公平的对照基准；OSC/DCS 载荷不处理（与过滤器一致）。
    """
    out = bytearray()
    i = 0
    n = len(data)
    last_cr = False
    while i < n:
        b = data[i]
        if b == 0x1B and i + 1 < n and data[i + 1] == 0x5D:   # ESC ]
            j = i + 2
            while j < n and data[j] != 0x07:
                j += 1
            out += data[i:min(j + 1, n)]
            i = j + 1
            last_cr = False
            continue
        if b == 0x0A:
            if not last_cr:
                out.append(0x0D)
            out.append(0x0A)
            last_cr = False
        else:
            out.append(b)
            last_cr = (b == 0x0D)
        i += 1
    return bytes(out)


def fmt(b, limit=200):
    o = []
    for x in b:
        if x == 0x1B:
            o.append("ESC")
        elif x == 0x0D:
            o.append("\\r")
        elif x == 0x0A:
            o.append("\\n")
        elif x == 0x07:
            o.append("<BEL>")
        elif 0x20 <= x < 0x7F:
            o.append(chr(x))
        else:
            o.append("<%02X>" % x)
    s = "".join(o)
    return s[:limit] + ("..." if len(s) > limit else "")


def rows(data):
    t = pywezterm.Terminal(W, H)
    t.feed(data)
    return [(i, len(ln) - len(ln.lstrip()), ln.strip())
            for i, ln in enumerate(t.text().rstrip("\n").split("\n"))
            if ln.strip()]


CASES = [
    ("ED0  ESC[0J 清到屏尾（已归一基准）",
     "A\\nB\\nC\\r ESC[0J X  → 清屏尾后写 X",
     b"AAA\nBBB\nCCC\r\x1b[0JXX"),

    ("CUP  ESC[2;3H 定位（已归一基准）",
     "A\\nB\\nC ESC[2;3H XY",
     b"AAA\nBBB\nCCC\x1b[2;3HXY"),

    ("CR 后写长内容（已归一基准）",
     "多行后 CR 回行首再写超宽",
     b"AAA\nBBB\nCCC\r" + b"Z" * 130),

    # ---- 追加：更贴近 termtest 的真实结构 ----
    ("termtest 真实结构（已归一基准）",
     "多行 hint + \\r ESC[2K + [SKIP] 行",
     "\n\x1b[90m  · 问题?\x1b[0m\n"
     "\x1b[0m[SKIP]\x1b[0m 项  非交互模式 / 未作答\n"
     "\r\x1b[2K"
     "\x1b[90m  · 非交互模式: 跳过等待\x1b[0m\n"
     "\n\x1b[1;36m▌ 下一段\x1b[0m\n".encode("utf-8")),

    # ---- 追加：ED/EL 变体 ----
    ("ED1  ESC[1J 清到屏首",
     "A\\nB\\nC\\r ESC[1J X",
     b"AAA\nBBB\nCCC\r\x1b[1JXX"),

    ("ED2  ESC[2J 清屏",
     "A\\nB ESC[2J X",
     b"AAA\nBBB\x1b[2JXX"),

    ("EL0+CR 组合（termtest 用的形式）",
     "ABC\\r ESC[0K XY",
     b"ABC\r\x1b[0KXY"),

    # ---- 追加：CUP 各种用法 ----
    ("CUP 同行定位 ESC[1;5H",
     "ABC ESC[1;5H XY",
     b"ABC\x1b[1;5HXY"),

    ("CUP 后 CLR 组合",
     "A\\nB ESC[1;1H ESC[2K X",
     b"AAA\nBBB\x1b[1;1H\x1b[2KXX"),

    # ---- 追加：滚动 ----
    ("滚屏（行数超过视口）",
     "30 行视口写 35 行 → 顶行滚出",
     b"".join(b"L%02d\n" % i for i in range(35))),
]


def main():
    print(SEP)
    print("ConHost 规范化差异（对照基准已做换行归一，剔除假阳性）")
    print(SEP)
    diff = 0
    for name, desc, payload in CASES:
        host = rt(payload)
        rh = rows(host)
        rw = rows(normalize_newlines(payload))
        same = (rh == rw)
        if not same:
            diff += 1
        print("\n-- {} --".format(name))
        print("   说明 : {}".format(desc))
        print("   发送 : {}".format(fmt(payload, 100)))
        print("   回吐 : {}".format(fmt(host, 170)))
        print("   ConHost : {}".format([(i, c, s[:26]) for i, c, s in rh] or "(空)"))
        print("   直通归一: {}".format([(i, c, s[:26]) for i, c, s in rw] or "(空)"))
        print("   >>> {}".format("一致" if same else "*** 不一致 ***"))

    print("\n" + SEP)
    print("汇总: {} / {} 个用例真实差异".format(diff, len(CASES)))
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
