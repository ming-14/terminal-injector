# -*- coding: utf-8 -*-
r"""runpy_repro_probe.py —— 直接拿用户的 run.py 当注入子进程跑，还原目标终端屏幕

用户现场（2026-09-24 后续报告）两个症状同时出现：
  (1) **第一条分栏线前有极长的空格**（说明输出前补发的 CursorPosition 的 X 极大）
  (2) `  · 窗口 120x30 | WT_SESSION=1` 与 `  · 查询探测:` 之间**凭空多出 26 行空行**
      （那一段正是 run.py 的 probe()：T.query() 里 Raw() + 发 DA1 查询 + 读回包）

本探针不做任何合成：直接跑环境变量 `TI_RUNPY` 指定的那个交互脚本（如某个带菜单的
测试套件入口），用 pyte 把目标终端的字节还原成屏幕，打印每行的"前导空格数"与空行区间，
于是"极长空格"和"凭空多出的空行"都变成可复读的数字。原生（不注入）作对照。

不内置任何个人路径 —— 要复现请显式指定：
    TI_RUNPY="/path/to/menu.py" python runpy_repro_probe.py
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
# 要复现的那个交互脚本（带菜单/提示符的程序）。**不内置个人路径**，由环境变量指定。
RUNPY = os.environ.get("TI_RUNPY", "")


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


def render(data, cols=120, rows=30):
    import pyte
    text = data.decode("utf-8", "replace")
    text = re.sub(r"\x1b[P_X^][^\x1b]*(?:\x1b\\)?", "", text, flags=re.S)
    text = re.sub(r"\x1b\[<u", "", text)
    # 必须带 history：症状在"流的顶部"（第一条分栏线被推到极右），只渲染可见屏幕
    # 会把它滚出去而看不见（技能 terminal-ansi-output-probe §4：
    # 要量"滚动缓冲 + 可见屏幕"，且分区快照）
    screen = pyte.HistoryScreen(cols, rows, history=800, ratio=0.5)
    pyte.Stream(screen).feed("\x1b[20h" + text)

    def row_text(row):
        # pyte 的 history.top 是 deque[dict[列号 -> Char]]，需自己拼成字符串
        if not isinstance(row, dict):
            return str(row).rstrip()
        if not row:
            return ""
        width = max(row) + 1
        return "".join(
            (row[c].data if c in row else " ") for c in range(width)).rstrip()

    hist = [row_text(r) for r in screen.history.top]
    return hist + [ln.rstrip() for ln in screen.display]


def report(label, lines):
    print("\n[{}] 还原屏幕（120x30）".format(label))
    for i, ln in enumerate(lines, 1):
        lead = len(ln) - len(ln.lstrip(" "))
        mark = "  ←前导空格 {}！".format(lead) if lead >= 40 else ""
        print("  {:2d}|{}".format(i, ln[:96] + mark if len(ln) > 96 else ln + mark))
    # 连续空行区间
    runs, start = [], None
    for i, ln in enumerate(lines):
        if not ln.strip():
            if start is None:
                start = i
        elif start is not None:
            runs.append((start + 1, i))
            start = None
    if start is not None:
        runs.append((start + 1, len(lines)))
    for a, b in runs:
        if b - a + 1 >= 3:
            print("  → 连续空行: 第 {}-{} 行（{} 行）".format(a, b, b - a + 1))


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
            label = "注入子进程"
        else:
            term = pty
            label = "原生子进程（对照）"

        term.write(list(('python "%s"\r' % RUNPY).encode("utf-8")))
        # 分区快照（技能 terminal-ansi-output-probe §4）：
        # run.py 的 print_menu 开头会 T.clear()（CSI 2J）把 T.start 那段擦掉，
        # 统一在最后量会把"极长空格"那一屏的证据一起抹掉 —— 所以命令后立刻量一次。
        phase1 = _drain(term, 3.5)
        report(label + " · 阶段1（命令后立刻）", render(phase1))
        phase2 = _drain(term, 13.0)
        allb = phase1 + phase2
        report(label + " · 阶段2（跑完菜单）", render(allb))
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "_tracker", "runpy_%s.bin" % ("inj" if inject else "nat")), "wb") as f:
                f.write(allb)
        except OSError:
            pass
        # 定位"窗口…"那一行与"查询探测"那一行之间的原始字节：
        # 26 行空行要么是 26 个换行、要么是一次大跨度 CUP，字节一看就知道
        a = allb.find("TERM=dumb".encode("utf-8"))
        b = allb.find("查询探测".encode("utf-8"))
        if a >= 0 and b > a:
            print("\n[{}] 两行之间的原始字节（{} 字节）:".format(label, b - a))
            print("  {!r}".format(bytes(allb[a:b])))
        return allb
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
    if not RUNPY or not os.path.exists(RUNPY):
        print("请先用环境变量 TI_RUNPY 指定要复现的交互脚本路径（本探针不内置任何个人路径）")
        return 1
    print("=" * 96)
    print("直接跑用户的 run.py：原生对照 vs 注入")
    print("=" * 96)
    _case(False, pywezterm)
    _case(True, pywezterm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
