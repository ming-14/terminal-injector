# -*- coding: utf-8 -*-
"""对比 ConHost 真值与注入链，验证「Enter 回显空行」一致（termtest 16 色段尾部）。

用户现象：WT 劫持后 termtest 16 色段，
  `· 16 色 ... 是否都能正确区分?   [y=是 / n=否]`
  与
  `[SKIP] 16色可区分 ...`
之间少了一个空行。

根因：ConHost 在 LINE_INPUT+ECHO_INPUT 下回显每个按键，Enter → `\\r\\n`。
注入链的 ReadFile(stdin) 分支此前不回显，故 Enter 的回显空行丢失。

本探针跑两遍同一份交互式尾部脚本：
  A. 裸 ConHost（pywezterm.Pty 直接 spawn）
  B. 注入链（TestSession + WT）
各自喂 `\\r`(只读 1 字节时会话立即完成)，把回吐/渲染按行对齐，检查
「问句行」与「[SKIP] 行」之间是否存在空行，且两路一致。
"""
import os, re, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "reference"))
sys.path.insert(0, os.path.join(ROOT, "tests", "e2e"))
import pywezterm
from common.session import TestSession

TAIL = r'''
# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, r"C:\Users\rikka\Desktop\测试集\集成\termtest")
import termlib as T

def _cue(text):
    T.wait_key(text + " — 按任意键继续")

def _ask_row(name, question, detail=""):
    r = T.ask_yn(question)
    if r is None:
        T.row(name, T.SKIP, detail or "非交互模式 / 未作答")
    else:
        T.row(name, T.PASS if r else T.FAIL, detail or question)

T.wln("")
T.hint("前景 + 背景 同时使用 (前景 30+i / 背景 40+i 对角线):")
line = "  "
for i in range(4):
    line += T.sgr(37 - i, 40 + i) + " %d/%d " % (37 - i, 40 + i) + T.sgr(0) + " "
T.wln(line)
T.wln("")
_ask_row("16色可区分", "16 色 (标准 8 + 亮 8) 是否都能正确区分?")
_cue("观察完 16 色")
import time
time.sleep(1.2)
'''

# ---------- 渲染 ----------
def render_lines(raw_bytes, cols=100):
    """把 VT 字节流渲染成屏幕行（够用：CR/LF/BS/CSI H/CUP/EL）。"""
    screen = [[" "] * cols]
    x = y = 0
    i = 0
    b = raw_bytes
    def ensure(yy):
        while len(screen) <= yy:
            screen.append([" "] * cols)
    while i < len(b):
        c = b[i]
        if c == 0x1B and i + 1 < len(b) and b[i+1] == 0x5B:  # ESC [
            j = i + 2
            params = b""
            while j < len(b) and 0x20 <= b[j] <= 0x3F:
                params += bytes([b[j]]); j += 1
            while j < len(b) and 0x20 <= b[j] <= 0x2F:
                j += 1
            if j < len(b):
                fin = b[j]; j += 1
                p = params.decode("latin1", "replace")
                if fin == ord('H') or fin == ord('f'):
                    parts = p.split(";")
                    ry = int(parts[0]) - 1 if parts and parts[0] else 0
                    rx = int(parts[1]) - 1 if len(parts) > 1 and parts[1] else 0
                    y = max(0, ry); x = max(0, rx); ensure(y)
                elif fin == ord('K'):
                    ensure(y)
                    for k in range(x, cols):
                        screen[y][k] = " "
                elif fin == ord('A'):
                    y = max(0, y - (int(p) if p else 1))
                elif fin == ord('B'):
                    y = y + (int(p) if p else 1); ensure(y)
                elif fin == ord('C'):
                    x = min(cols - 1, x + (int(p) if p else 1))
                elif fin == ord('D'):
                    x = max(0, x - (int(p) if p else 1))
                i = j
                continue
            i += 1
            continue
        elif c == 0x1B:
            i += 2  # 简单跳过 ESC + 1
            continue
        elif c == 0x0D:
            x = 0
        elif c == 0x0A:
            y += 1; ensure(y)
        elif c == 0x08:
            x = max(0, x - 1)
        else:
            ensure(y)
            if x < cols:
                try:
                    ch = bytes([c]).decode("utf-8")
                except Exception:
                    ch = None
            else:
                ch = None
            if ch is None and c >= 0x80:
                # 多字节 UTF-8：整体尝试
                k = 1
                while i + k < len(b) and k < 4 and (b[i+k] & 0xC0) == 0x80:
                    k += 1
                try:
                    ch = b[i:i+k].decode("utf-8")
                except Exception:
                    ch = "?"
                i += k - 1
            elif ch is None:
                ch = chr(c)
            if x < cols and ch:
                screen[y][x] = ch
                x += len(ch) if len(ch) == 1 else 1
        i += 1
    return ["".join(r).rstrip() for r in screen]


# ---------- A: 裸 ConHost ----------
tmp = os.path.join(HERE, "_ee_tail.py")
with open(tmp, "w", encoding="utf-8") as f:
    f.write(TAIL)

p = pywezterm.Pty(100, 30)
p.spawn([sys.executable, tmp])
buf_a = b""
t0 = time.time(); last = t0; sent = False
while time.time() - last < 4.0:
    c = p.read(8192, 0.3)
    if c:
        last = time.time(); buf_a += c
    if not sent and time.time() - t0 > 0.8:
        try:
            p.write(b"\r")   # 只按 Enter：ask_yn 读到 1 字节 \r 立即完成
            sent = True
        except Exception as e:
            print("A write err", e)
    elif p.try_wait() is not None:
        while True:
            c = p.read(8192, 0.3)
            if not c:
                break
            buf_a += c
        break
p.close()

# ---------- B: 注入链 ----------
import uuid
NAME = "ee_consist"
from common import result as result_mod
result_mod.clear_result(NAME)
body = TAIL.replace("time.sleep(1.2)", "time.sleep(1.2)")
# 目标内置结果记号，便于判定结束
body = "import sys\n" + body + "\n"

with TestSession() as s:
    s.run_target(NAME, body, ready_key=None, ready_timeout=30.0)
    time.sleep(1.2)
    s.type_enter()
    time.sleep(2.5)
    txt = s.log().read_all()

# 从注入日志取 ChildVtOutput 字节
pat = re.compile(r"ChildVtOutput: len=\d+ written=\d+ ok=\d err=\d hex\[\d+\]=\s*(.*?)\s*$")
buf_b = bytearray()
for line in txt.splitlines():
    m = pat.search(line)
    if m:
        buf_b += bytes(int(hh, 16) for hh in m.group(1).split())

lines_a = render_lines(buf_a)
lines_b = render_lines(bytes(buf_b))

def find(lines, needle):
    for idx, ln in enumerate(lines):
        if needle in ln:
            return idx
    return -1

print("=" * 70)
print("B 全屏（前 40 行）：")
for k, ln in enumerate(lines_b[:40]):
    print("  B[%d] %r" % (k, ln))
print("-" * 70)
print("A. ConHost 真值 —— 问句行及其后 3 行：")
ia = find(lines_a, "是否都能正确区分")
for k in range(ia, min(ia + 4, len(lines_a))):
    print("  A[%d] %r" % (k, lines_a[k]))
print("B. 注入链 —— 问句行及其后 3 行：")
ib = find(lines_b, "是否都能正确区分")
for k in range(ib, min(ib + 4, len(lines_b))):
    print("  B[%d] %r" % (k, lines_b[k]))

ia_skip = find(lines_a, "[SKIP]")
ib_skip = find(lines_b, "[SKIP]")
print("-" * 70)
print("A: 问句行=%d, [SKIP]行=%d, 间隔=%d" % (ia, ia_skip, ia_skip - ia))
print("B: 问句行=%d, [SKIP]行=%d, 间隔=%d" % (ib, ib_skip, ib_skip - ib))
consistent = (ia_skip - ia) == (ib_skip - ib) and ia >= 0 and ib_skip >= 0
print("结论: %s" % ("一致 ✔（间隔均为 2，即含一个空行）" if consistent else "不一致 ✘"))
