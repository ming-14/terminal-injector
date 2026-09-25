# -*- coding: utf-8 -*-
"""t_cr_erase_min.py —— 最小对照：裸 ConPTY vs 注入链路，对同一段字节的规范输出

只做一件事：把同一段含 `\r\x1b[2K` 的字节，
  A) 喂进【裸 ConPTY】(pywezterm.Pty，无注入)
  B) 喂进【注入链路】(真 ConPTY 承载 mediator + 被注入目标)
用 pywezterm.Terminal 还原两者屏幕，比对行结构。

判据：A 与 B 的行结构应一致。若 B 丢了空行 → 定位到注入链路。

    python t_cr_erase_min.py
"""
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
    os.path.join(HERE, "..", ".."))
os.environ.setdefault("TI_PROJECT_ROOT", PROJECT_ROOT)
PW_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PW_DIR not in sys.path:
    sys.path.insert(0, PW_DIR)
SKILL_SCRIPTS = os.environ.get("TI_PWTERM_SCRIPTS") or os.path.expanduser(
    r"~\.workbuddy\skills\pywezterm-terminal-probe\scripts")
if os.path.isdir(SKILL_SCRIPTS) and SKILL_SCRIPTS not in sys.path:
    sys.path.insert(0, SKILL_SCRIPTS)

BUILD_BIN = os.path.join(PROJECT_ROOT, "build", "bin", "Release")
LOG_DIR = os.path.join(BUILD_BIN, "logs")
SEP = "=" * 74

Q = "256 色块是否都能区分且无错位?"
PAYLOAD = ("\n"
           "\x1b[90m  · " + Q + "  → 非交互模式: 记 SKIP\x1b[0m\n"
           "\x1b[0m[SKIP]\x1b[0m 256色可区分  非交互模式 / 未作答\n"
           "\r\x1b[2K"
           "\x1b[90m  · 非交互模式: 跳过等待\x1b[0m\n"
           "\n\x1b[1;36m▌ 真彩色 24bit\x1b[0m\n").encode("utf-8")

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


def lines_of(text):
    return text.rstrip("\n").split("\n")


def show(tag, text):
    print("\n  [{}] 行结构:".format(tag))
    for i, ln in enumerate(lines_of(text)):
        print("    {:>2}| {!r}{}".format(i, ln, "  <== 空行" if ln == "" else ""))


def raw_conpty(payload, timeout=3.0):
    import pywezterm
    p = pywezterm.Pty(120, 40)
    p.spawn([sys.executable, "-c", _HELPER, payload.hex()])
    out = b""
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


def main():
    import pywezterm
    print(SEP)
    print("裸 ConPTY vs 注入链路：同一段 `\\r ESC[2K` 字节的行结构")
    print(SEP)
    print("\n  发送字节（{} bytes）:".format(len(PAYLOAD)))
    print("   ", " ".join("{:02X}".format(x) for x in PAYLOAD))

    # A) 裸 ConPTY
    a_raw = raw_conpty(PAYLOAD)
    ta = pywezterm.Terminal(120, 40)
    ta.feed(a_raw)
    show("A 裸 ConPTY", ta.text())

    # B) 注入链路
    try:
        from pwterm import PwSession
    except Exception as e:
        print("\n  ! 无 PwSession，跳过 B: {}".format(e))
        return 0
    os.makedirs(LOG_DIR, exist_ok=True)
    script = os.path.join(LOG_DIR, "cr_min_payload.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write("import os,sys,time\nsys.stdout.reconfigure(encoding='utf-8')\n"
                "os.write(1, bytes.fromhex(%r))\nsys.stdout.flush()\ntime.sleep(12)\n"
                % PAYLOAD.hex())
    with PwSession(cols=120, rows=40) as s:
        if not s.start("ps7"):
            print("  ✗ 握手失败")
            return 1
        print("\n  [setup] OK target_pid={} mediator_pid={}".format(
            s.target_pid, s.mediator_pid))
        s.drain(1.5)
        s.write_line('python "{}"'.format(script))
        # 轮询等待 payload 完成：命令下发 + python 冷启动 + 写入有较大抖动，
        # 固定 sleep 会偶发抓不到（只看到 prompt）。等含 "真彩色" 的输出出现。
        out = b""
        deadline = time.time() + 25.0
        while time.time() < deadline:
            s.drain(0.5)
            out = s.all_output()
            if "真彩色".encode("utf-8") in out:
                break
        s.drain(1.0)
        tb = pywezterm.Terminal(120, 40)
        tb.feed(s.all_output())
        show("B 注入链路", tb.text())

    la, lb = lines_of(ta.text()), lines_of(tb.text())
    # B 侧首行是命令回显（A 侧无），从尾部对齐比较内容区。
    n = min(len(la), len(lb))
    tail_a, tail_b = la[-n:], lb[-n:]
    same = (tail_a == tail_b)
    print("\n  判据: A 行数={} B 行数={}（B 首行含命令回显）".format(len(la), len(lb)))
    print("        内容区末尾 {} 行对齐: {}".format(n, "一致" if same else "不一致"))
    if not same:
        for i, (x, y) in enumerate(zip(tail_a, tail_b)):
            if x != y:
                print("        首个差异 @{}: A={!r} B={!r}".format(i, x, y))
                break
    print("\n" + SEP)
    print("DONE")
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
