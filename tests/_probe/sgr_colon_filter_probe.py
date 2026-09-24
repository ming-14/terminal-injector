# -*- coding: utf-8 -*-
"""sgr_colon_filter_probe.py —— 真彩色冒号写法在 DLL SGR 过滤器下的字节变化

背景
----
用户现场（tipwsh.exe 14916 注入后跑 t_sgr.py）：
  "真彩色 24bit" 小节里 `CSI 38:2::r:g:b` 那条渐变条出现错色块，
  而 `CSI 38;2;r;g;b` / `CSI 38;2;r:g:b` 两条正常。

怀疑
----
DLL VT 直通路径上有 `VtSgrFilter`（剥离 ConHost 无法表达的 SGR 9/29 删除线），
它把 ':' 与 ';' 都当分隔符 tokenize，重建时又统一用 ';' 拼接：
    ESC[38:2::128:255:128m  ->  ESC[38;2;;128;255:128m   （多一个空参数）
    ESC[38:2::128:255:9m    ->  ESC[38;2;;128;255m       （B 通道被当删除线剥离）

本探针做两件事
--------------
1. 复刻 RebuildSgr 的改写规则，打印三种写法的改写结果；
2. 用真 ConPTY（pywezterm Pty，侧载 OpenConsole.exe）分别喂入"程序原始字节"
   与"DLL 过滤后字节"，读回 ConPTY 规范化后的输出字节，直接看解析是否发散。

    python sgr_colon_filter_probe.py
"""
import os
import sys

_REF = os.environ.get("PWTERM_DIR") or os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "reference"))
if _REF not in sys.path:
    sys.path.insert(0, _REF)
import pywezterm  # noqa: E402


# ---------------------------------------------------------------- 1. 复刻改写
def rebuild_sgr(params):
    """按 src/dll/translator/VtSgrFilter.cpp::RebuildSgr 逐行复刻。"""
    tokens, cur = [], ""
    for c in params:
        if c == ";" or c == ":":
            tokens.append(cur)
            cur = ""
        else:
            cur += c
    tokens.append(cur)

    kept, expect_mode, left = [], False, 0
    for t in tokens:
        if left > 0:
            kept.append(t)
            left -= 1
            continue
        if expect_mode:
            try:
                mode = int(t)
            except ValueError:
                mode = 0
            left = {5: 1, 2: 3, 4: 4}.get(mode, 1)
            kept.append(t)
            expect_mode = False
            continue
        if t in ("38", "48", "58"):
            kept.append(t)
            expect_mode = True
            continue
        if t in ("9", "29"):
            continue
        kept.append(t)

    if len(tokens) == 1 and tokens[0] == "":
        return "\x1b[m"
    if not kept:
        return None
    return "\x1b[" + ";".join(kept) + "m"


def show_rewrite():
    print("=" * 74)
    print("1) 复刻 VtSgrFilter::RebuildSgr —— 直通字节在 DLL 内被怎么改写")
    print("=" * 74)
    cases = [
        ("分号式        ", "38;2;128;255;128"),
        ("混用式        ", "38;2;128:255:128"),
        ("冒号带保留位  ", "38:2::128:255:128"),
        ("冒号无保留位  ", "38:2:128:255:128"),
        ("冒号带保留位·B=9", "38:2::128:255:9"),
    ]
    for name, p in cases:
        out = rebuild_sgr(p)
        flag = "" if out == "\x1b[%sm" % p else "   <== 被改写"
        print("  %-16s ESC[%-22sm -> %r%s" % (name, p, out, flag))
    print()


# ---------------------------------------------------------------- 2. 真 ConPTY
_HELPER = r"""
import ctypes, os, sys
k = ctypes.windll.kernel32
h = k.GetStdHandle(-11)
m = ctypes.c_ulong(0)
k.GetConsoleMode(h, ctypes.byref(m))
k.SetConsoleMode(h, m.value | 0x0004)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
os.write(1, bytes.fromhex(sys.argv[1]))
"""


def conpty_roundtrip(payload: bytes, timeout=3.0) -> bytes:
    """把 payload 写进自己的 stdout（真 ConPTY），读回 ConPTY 规范化后的输出。"""
    p = pywezterm.Pty(80, 24)
    p.spawn([sys.executable, "-c", _HELPER, payload.hex()])
    out = b""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = p.read(4096, 0.2)
        if c:
            out += c
        elif p.try_wait() is not None:
            while True:
                c = p.read(4096, 0.3)
                if not c:
                    break
                out += c
            break
    p.close()
    return out


def show_conpty():
    print("=" * 74)
    print("2) 真 ConPTY 往返：程序原始字节 vs DLL 过滤后字节，颜色解析是否发散")
    print("=" * 74)

    samples = [("t=0.30", (77, 153, 179)), ("t=0.50", (128, 255, 128)),
               ("t=0.70", (179, 153, 77))]
    for tag, (r, g, b) in samples:
        raw_colon = "\x1b[38:2::%d:%d:%dm" % (r, g, b)
        filtered = rebuild_sgr(raw_colon[2:-1]) or ""
        cases = [
            ("A 程序原始·冒号带保留位", raw_colon),
            ("B DLL 过滤后(=A 经过滤器)", filtered),
            ("C 程序原始·分号式(对照)", "\x1b[38;2;%d;%d;%dm" % (r, g, b)),
        ]
        print("\n  --- %s  rgb(%d,%d,%d) ---" % (tag, r, g, b))
        for name, seq in cases:
            payload = (seq + "X\x1b[0m").encode()
            got = conpty_roundtrip(payload)
            print("    %-28s 输入 %-30r" % (name, seq))
            print("      ConPTY 回吐 %r" % got)
    print()


def main():
    show_rewrite()
    show_conpty()
    return 0


if __name__ == "__main__":
    sys.exit(main())
