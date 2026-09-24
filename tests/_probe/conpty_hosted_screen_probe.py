# -*- coding: utf-8 -*-
r"""conpty_hosted_screen_probe.py —— 还原「ConPTY 托管目标」注入后目标终端上的真实画面与光标

用途
----
BUG-013 修好"空白屏"之后，用户报告**光标位置不对**：
画面是
    PowerShell 7.6.3
    ...
    |PS C:\Users\<whoami>>        ← "|" 是光标位置（示意，用户名已脱敏）
即光标停在 prompt **行首**，应该在 prompt 末尾。

本探针把目标终端（mediator 的 stdout，即 ConPTY）收到的**原始字节**喂给 pyte 终端模型，
还原成屏幕文本 + 光标坐标，于是"光标在第几行第几列"变成一个可断言的数值，
不用靠肉眼看窗口。

场景与用户一致：目标 pwsh 跑在一个 ConPTY 里（源 150x40），**不敲任何命令**（就停在
刚启动的 prompt 上），然后注入到另一个 ConPTY（目标终端 120x30）。

同时打印：
  - DLL 日志里的 cursor / KickStart 相关行（看"重印 prompt"的前提有没有发生）
  - 目标字节里 "PS " 出现次数（1 次=没重印，2 次=重印了）

    python conpty_hosted_screen_probe.py
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "e2e"))

from common.paths import PROJECT_ROOT, BUILD_BIN, ti_log_path
from common import childlog

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)

PS7_EXE = "".join(["p", "w", "s", "h", ".exe"])
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")

COLS, ROWS = 120, 30


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


def render(data: bytes):
    """把字节流喂给 pyte，返回 (可断言行列表, 光标(x,y) 1-based, 说明)。

    按技能 terminal-ansi-output-probe §2/§7 的对齐要求：
      - 剥掉字符串型序列（DCS/APC/PM/SOS），pyte 会把 payload 当文本打出来
      - 剥掉 CSI <u（kitty 键盘协议 pop，pyte 不认，会把 u 打出来并拆散宽字符）
      - 开 LNM（CSI 20h）：Windows 控制台 \n 按 CR+LF 处理，pyte 默认只下移
    """
    try:
        import pyte
    except ImportError:
        return None, None, "pyte 未安装（pip install pyte）"

    text = data.decode("utf-8", "replace")
    text = re.sub(r"\x1b[P_X^][^\x1b]*(?:\x1b\\)?", "", text, flags=re.S)
    text = re.sub(r"\x1b\[<u", "", text)
    text = "\x1b[20h" + text

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    stream.feed(text)
    lines = [ln.rstrip() for ln in screen.display]
    return lines, (screen.cursor.x + 1, screen.cursor.y + 1), "ok"


def show(lines, cur):
    print("  目标终端 120x30 还原画面（'^' 标出光标列）:")
    for i, ln in enumerate(lines):
        mark = " " * (cur[0] - 1) + "^" if (cur[1] - 1) == i else ""
        tag = "{:2d}|".format(i + 1)
        print("    {}{}".format(tag, ln[:110]))
        if mark:
            print("      {}|{}".format(" " * 3, mark))
    print("    → 光标 (列,行) 1-based = {}".format(cur))


def main():
    try:
        import pywezterm
    except ImportError as e:
        print("无法 import pywezterm (PWTERM_DIR={!r}): {}".format(PWTERM_DIR, e))
        return 1

    t_pty = d_pty = None
    target_pid = 0
    try:
        print("=" * 78)
        print("ConPTY 托管目标：注入后目标终端的画面与光标")
        print("=" * 78)

        # 1. 目标：pwsh 跑在 ConPTY 里，**不敲命令**（就停在刚启动的 prompt）
        t_pty = pywezterm.Pty(150, 40)
        target_pid, _ = t_pty.spawn([PS7_EXE, "-NoLogo"])
        print("[setup] 目标 pid={}（ConPTY 托管，源 150x40，不敲命令）".format(target_pid))
        src = _drain(t_pty, 4.0)
        lines, cur, note = render(src)
        print("[setup] 源终端画面（注入前）:")
        if lines:
            show(lines, cur)
        else:
            print("    {}".format(note))

        # 2. 目标终端：mediator 跑在另一个 ConPTY 里
        d_pty = pywezterm.Pty(COLS, ROWS)
        med_pid, _ = d_pty.spawn([MEDIATOR_EXE, "--mediator",
                                  "--target-pid", str(target_pid)])
        print("\n[setup] mediator pid={}（目标终端 120x30）".format(med_pid))

        mlog = ti_log_path(target_pid)
        t0 = time.time()
        while time.time() - t0 < 25 and "Handshake OK" not in _read(mlog):
            time.sleep(0.2)
        print("[setup] 握手 {}（{:.1f}s）".format(
            "OK" if "Handshake OK" in _read(mlog) else "失败", time.time() - t0))

        out = _drain(d_pty, 5.0)
        lines, cur, note = render(out)
        print("\n[结果] 目标终端收到 {} 字节".format(len(out)))
        if lines:
            show(lines, cur)
        else:
            print("    {}".format(note))

        # 3. 注入后还能不能打字（关键：光标只是表象，输入通路才是要紧的）
        probe = "echo TI_TYPED_OK\r"
        d_pty.write(list(probe.encode("utf-8")))
        out2 = _drain(d_pty, 6.0)
        # 键入的字符会被目标终端回显（ECHO 模式）→ 看回显里有没有我们打的字
        echoed = b"echo TI_TYPED_OK" in out2
        ran = b"TI_TYPED_OK" in out2.replace(b"echo TI_TYPED_OK", b"", 1)
        print("\n[输入] 注入后向目标终端键入 {!r}".format(probe.strip()))
        print("  → 键入字符被回显: {}（本段共 {} 字节）".format(echoed, len(out2)))
        print("  → 命令被执行（出现第二处标记）: {}".format(ran))
        lines, cur, note = render(out + out2)
        if lines:
            print("  键入后画面:")
            show(lines, cur)

        # 4. 用日志说明"重印 prompt"的前提有没有发生
        dll = _read(childlog.latest_injected_log(target_pid))
        print("\n[日志] DLL 侧 cursor / KickStart:")
        for ln in dll.splitlines():
            if any(k in ln for k in ("cursor", "Cursor", "KickStart", "prompt")):
                i = ln.find("]")
                print("    " + (ln[i + 1:] if i >= 0 else ln).strip()[:150])

        print("\n[计数] 目标字节中 'PS ' 出现 {} 次（1=没重印 prompt，2=重印了）".format(
            (out + out2).count(b"PS ")))
        print("[原始尾部] {!r}".format(bytes(out[-160:])))
    finally:
        for p in (d_pty, t_pty):
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
