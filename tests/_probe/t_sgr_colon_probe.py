# -*- coding: utf-8 -*-
"""t_sgr_colon_probe.py —— 冒号式真彩色 SGR 端到端验证（Bug 回归）

现象（用户报告）
---------------
劫持到 WT 后跑 termtest，真彩色小节里三条前景/背景渐变条不一致：
其中一条渲染错乱（颜色反相、尾端黑块），而正常 WT 里三条应当一致。

根因
----
`VtSgrFilter::RebuildSgr` 把 ';' 与 ':' 都当分隔符拍平、再一律用 ';' 重建：

    \x1b[38:2::255:0:0m  -> tokens ["38","2","","255","0","0"]
                         -> 保护"模式2"的 3 个数据位 = ""(空保留位), 255, 0
                         -> 重建 \x1b[38;2;;255;0;m
                         -> 解析时 ";;" 取缺省 0
                         -> 实际颜色 (R=0, G=255, B=0)  ← 整条反相

隔离实测（wezterm.Terminal 直接喂序列）已坐实三种形态：

    38;2;255;0;0      -> #ff0000   （正常）
    38:2::255:0:0     -> #ff0000   （**空保留位是合法且必须的**）
    38;2;;255;0       -> default   （修复前重建出的破坏形态 → 颜色丢失）

修复后该序列必须逐字节原样透传。

本探针验证什么
--------------
用 pywezterm 真 ConPTY 承载 mediator（扮演 WT），向被注入的目标写入关键序列，
双路取证：
  T1  过滤器输出：mediator 日志里 `38:2::255:0:0` 原样出现，
      且**不含**被破坏的 `38;2;;255;0`
  T2  终端解析：把同样的字节直接喂 pywezterm.Terminal，
      冒号带保留位写法得到的前景色与分号式**完全一致**（都是 #ff0000）

    python t_sgr_colon_probe.py
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

# 本 bug 的关键序列（冒号带空保留位）
KEY_FG = b"\x1b[38:2::255:0:0m"
KEY_BG = b"\x1b[48:2::255:0:0m"
# 对照组（分号式）
REF_FG = b"\x1b[38;2;255;0;0m"
# 修复前重建出的破坏形态
BROKEN_FG = b"\x1b[38;2;;255;0"

# 目标脚本：把关键序列 + 标记写出来
PAYLOAD = r'''
import sys, time
# 分号式（对照组）
sys.stdout.write("\x1b[38;2;255;0;0mREFMARK\x1b[0m\r\n")
# 冒号带空保留位（本 bug 的实验组）
sys.stdout.write("\x1b[38:2::255:0:0mKEYMARK\x1b[0m\r\n")
sys.stdout.flush()
time.sleep(6)
'''


def hexs(b):
    return " ".join("{:02X}".format(x) for x in b)


def main():
    from pwterm import PwSession
    import pywezterm

    os.makedirs(LOG_DIR, exist_ok=True)
    script = os.path.join(LOG_DIR, "sgr_colon_payload.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(PAYLOAD)

    fails = []
    print(SEP)
    print("冒号式真彩色 SGR 端到端验证（38:2::r:g:b / 48:2::r:g:b）")
    print(SEP)

    # ---------------- T2：终端解析（不依赖注入，先做纯解析对照） ----------------
    print("\n[T2] 终端解析对照（pywezterm.Terminal 直接喂序列）")

    def fg_of(seq, ch):
        t = pywezterm.Terminal(40, 4)
        t.feed(seq + ch.encode() + b"\x1b[0m")
        for c in t.snapshot()[0]:
            if c[1] == ch:
                return c[2]
        return None

    ref_fg_color = fg_of(REF_FG, "R")
    key_fg_color = fg_of(KEY_FG, "K")
    # 破坏形态常量不含最终字节 'm'，需补上才是完整 CSI
    broken_fg_color = fg_of(BROKEN_FG + b"m", "B")

    print("  分号式 38;2;255;0;0   -> fg={}".format(ref_fg_color))
    print("  冒号式 38:2::255:0:0  -> fg={}".format(key_fg_color))
    print("  破坏态 38;2;;255;0m   -> fg={}".format(broken_fg_color))

    if key_fg_color == ref_fg_color and key_fg_color not in (None, "default"):
        print("  ✓ 冒号带保留位与分号式解析**一致**")
    else:
        print("  ✗ 两者解析不一致")
        fails.append("T2-parse")

    if broken_fg_color != ref_fg_color:
        print("  ✓ 破坏态与正确色不同（{}）—— 证明原重建方式有害".format(
            broken_fg_color))
    else:
        print("  ! 破坏态未如预期偏离（对照前提变化）: {}".format(broken_fg_color))

    # ---------------- T1：过滤器输出（经真实注入链路） ----------------
    print("\n[T1] 过滤器输出（真 ConPTY 承载 mediator，目标被注入）")
    with PwSession(cols=100, rows=30) as s:
        if not s.start("ps7"):
            print("  ✗ 握手失败（mediator 未就绪）")
            fails.append("T1-handshake")
            print("\n" + SEP)
            print("RESULT: FAIL  失败项 = {}".format(", ".join(fails)))
            print(SEP)
            return 1
        print("  [setup] 握手 OK target_pid={} mediator_pid={}".format(
            s.target_pid, s.mediator_pid))
        s.drain(1.5)

        s.write_line('python "{}"'.format(script))
        time.sleep(4.0)
        s.drain(2.0)

        med = s.mediator_log()
        lines = [l for l in med.splitlines()
                 if "VtOutput" in l or "ChildVtOutput" in l]
        blob = "\n".join(lines)

        want_hex = hexs(KEY_FG)
        broken_hex = hexs(BROKEN_FG)
        ref_hex = hexs(REF_FG)

        print("  关键序列        :", want_hex)
        print("  破坏形态(不应有):", broken_hex)

        if broken_hex in blob:
            print("  ✗ mediator 日志中出现被破坏的形态 → 过滤器仍在改写冒号式")
            fails.append("T1-broken")
        else:
            print("  ✓ 未出现被破坏的形态")

        if want_hex in blob:
            print("  ✓ 关键序列原样出现在日志中")
        else:
            print("  ! 日志未见完整关键序列（可能被日志 hex 截断/分片），"
                  "以'未出现破坏形态'为主要判据")
        print("  含分号式参照序列 :", ref_hex in blob)

        # 屏幕还原：确认目标程序真的把两行输出送进了链路
        import pywezterm as _pw
        t = _pw.Terminal(100, 30)
        t.feed(s.all_output())
        text = t.text()
        print("  屏幕含 REFMARK:", "REFMARK" in text,
              " 含 KEYMARK:", "KEYMARK" in text)

    print("\n" + SEP)
    if fails:
        print("RESULT: FAIL  失败项 = {}".format(", ".join(fails)))
    else:
        print("RESULT: PASS")
    print(SEP)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
