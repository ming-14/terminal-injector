# -*- coding: utf-8 -*-
r"""tracker_cursor_probe.py —— 逐构造测「VtCursorTracker 的光标」与真值是否一致

思路（关键：让 tracker 自己把坐标报出来）
----------------------------------------
子进程写内容前，DLL 会补发一条 `CursorSync` = `VtCursorTracker` 当时的坐标
（`OutputHooks.cpp: SyncChildVtCursorBeforeWrite`）。所以只要在子进程输出里放一个
标记，**解析标记前面那条 `ESC[<row>;<col>H` 就等于读到了 tracker 的光标** ——
不需要给 C++ 加日志、也不需要单测框架。

然后同一条字节流：
  · 原生（不注入）→ pyte 还原 = **真值**（终端实际在哪）；
  · 注入 → 上面解析出的 = **tracker 认为在哪**。
逐个构造比对，第一个出现差异的构造就是 tracker 跑偏的起点。

每个用例都先 `ESC[2J ESC[H` 清屏归位，再写 `CASE_x`，再发**待测构造**，再写标记 `Mx`。

    python tracker_cursor_probe.py
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "e2e"))

from common.paths import PROJECT_ROOT, BUILD_BIN, ti_log_path

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)

PS7_EXE = "".join(["p", "w", "s", "h", ".exe"])
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tracker")
SCRIPT = os.path.join(WORK, "seqs.py")

# 每个用例：名字 → 待测构造（原样字节）
CASES = [
    ("A", b""),                          # 基线：构造为空
    ("B", b"\x1b[K"),                    # EL(0)：不应移动光标
    ("C", b"\r\n"),                      # CRLF：下移一行
    ("D", b"\x1b[K\r\n"),                # EL + CRLF（run.py 清屏的原子单元）
    ("E", b"\x1b[K\r\n" * 5),            # 重复 5 次
    ("F", b"\x1b[K\r\n" * 26),           # 重复 26 次（run.py 的规模）
    ("G", b"\x1b[8;1H"),                 # CUP 绝对定位
    ("H", b"\x1b[8;1H\x1b[K"),           # CUP + EL
    ("I", b"\x1b[20;1H\r\n" * 3),        # 绝对定位 + 相对下移
    ("J", b"\r\n" * 20),                 # 纯 CRLF ×20（跨屏，应滚动）
]

SCRIPT_SRC = '''# -*- coding: utf-8 -*-
import os, sys, time
CASES = __CASES__
REAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "real_window.bin")
def emit(b):
    os.write(1, b); time.sleep(0.12)
for name, seq in CASES:
    emit(b"\\x1b[2J\\x1b[H")
    emit(("CASE_" + name + "\\r\\n").encode())
    if seq:
        emit(seq)
    emit(("M" + name).encode())
    emit(b"\\r\\n")
# 用 run.py 的**真实**字节窗口再测一次（从 native 流里切出来的那 1373 字节）
if os.path.exists(REAL):
    seq = open(REAL, "rb").read()
    emit(b"\\x1b[2J\\x1b[H")
    emit(b"CASE_REAL\\r\\n")
    emit(seq)
    emit(b"MREAL")
    emit(b"\\r\\n")
time.sleep(0.5)
'''


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _drain(pty, seconds, interval=0.1):
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        try:
            d = bytes(pty.read(65536, timeout=interval))
        except Exception:
            break
        if d:
            buf += d
    return buf


def trackers_from_bytes(data):
    """从字节流解析：每个标记 M<x> 前面最近的一条 CUP = tracker 当时坐标。"""
    out = {}
    for m in re.finditer(rb"M([A-Z])\r?\n", data):
        name = m.group(1).decode()
        head = data[:m.start()]
        cups = list(re.finditer(rb"\x1b\[(\d+);(\d+)H", head))
        if cups:
            out[name] = (int(cups[-1].group(1)), int(cups[-1].group(2)))
        else:
            out[name] = None
    return out


def truth_from_bytes(data):
    """原生侧：pyte 还原，逐标记读真实光标。注意**不开 LNM**（DLL 未设 LNM）。"""
    import pyte
    text = data.decode("utf-8", "replace")
    text = re.sub(r"\x1b[P_X^][^\x1b]*(?:\x1b\\)?", "", text, flags=re.S)
    text = re.sub(r"\x1b\[<u", "", text)
    screen = pyte.Screen(120, 30)
    st = pyte.Stream(screen)
    out = {}
    pos = 0
    for m in re.finditer(r"M([A-Z])\r?\n", text):
        st.feed(text[pos:m.start()])
        out[m.group(1)] = (screen.cursor.y + 1, screen.cursor.x + 1)
        st.feed(text[m.start():m.end()])
        pos = m.end()
    return out


def _case(inject, pywezterm):
    pty = d_pty = None
    target_pid = 0
    try:
        pty = pywezterm.Pty(120, 30)
        target_pid, _ = pty.spawn([PS7_EXE, "-NoLogo"])
        _drain(pty, 3.5)
        if inject:
            d_pty = pywezterm.Pty(120, 30)
            d_pty.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])
            mlog = ti_log_path(target_pid)
            t0 = time.time()
            while time.time() - t0 < 25 and "Handshake OK" not in _read(mlog):
                time.sleep(0.2)
            _drain(d_pty, 1.0)
            term = d_pty
        else:
            term = pty
        term.write(list(('python "%s"\r' % SCRIPT).encode("utf-8")))
        # 用例间有 0.12s 间隔 ×10 用例 ×2，等足
        return _drain(term, 30.0)
    finally:
        for p in (d_pty, pty):
            try:
                if p is not None:
                    p.close()
            except Exception:
                pass
        try:
            import psutil
            for proc in psutil.process_iter(["name"]):
                try:
                    if (proc.info["name"] or "").lower() == "terminal_injector.exe":
                        proc.terminate()
                except Exception:
                    pass
            if target_pid:
                psutil.Process(target_pid).terminate()
        except Exception:
            pass


def main():
    try:
        import pywezterm
    except ImportError as e:
        print("无法 import pywezterm: {}".format(e))
        return 1

    os.makedirs(WORK, exist_ok=True)
    with open(SCRIPT, "w", encoding="utf-8") as f:
        f.write(SCRIPT_SRC.replace("__CASES__", repr(CASES)))

    print("=" * 84)
    print("逐构造比对：tracker 报的坐标（注入，从 CursorSync 解出） vs 真值（原生 pyte）")
    print("=" * 84)
    nat = _case(False, pywezterm)
    inj = _case(True, pywezterm)
    # 落盘原始字节，便于离线精读（哪条 CUP 是同步、哪条是内容自带）
    try:
        with open(os.path.join(WORK, "native.bin"), "wb") as f:
            f.write(nat)
        with open(os.path.join(WORK, "injected.bin"), "wb") as f:
            f.write(inj)
        print("\n(原始字节已落盘: _tracker/native.bin, _tracker/injected.bin)")
    except OSError:
        pass

    t = truth_from_bytes(nat)
    k = trackers_from_bytes(inj)

    print("\n 用例  待测构造                       真值(行,列)   tracker(行,列)   一致?")
    for name, seq in CASES:
        desc = repr(seq)[2:-1][:30] if seq else "(空)"
        tt = t.get(name)
        kk = k.get(name)
        ok = "✓" if (tt and kk and tt == kk) else "✗"
        print("  {:<4}  {:<32} {:<12} {:<15} {}".format(
            name, desc, str(tt), str(kk), ok))

    print("\n 说明：真值行号超过 30（末行）时应被滚动/夹紧为 30；tracker 若报 >30 即为发散。")


if __name__ == "__main__":
    sys.exit(main())
