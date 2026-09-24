# -*- coding: utf-8 -*-
r"""child_echo_position_probe.py —— 量「子进程行编辑回显」落在屏幕的哪一格

背景（2026-09-24 用户报告）
--------------------------
修好「子进程 input() 挂住 / 无回显」之后，用户报告新症状：**输入通了、字出现在错误的位置**。
用户在 run.py 菜单里敲 `2`，`2` 没有出现在 ` 选择 >` 之后，而是出现在
第 7 项的**描述行第 0 列**，后面还留着一长串原有的缩进空格。

机制（读代码得出）：子进程（`!IsTargetProcess()`）在发回显字节**之前**会先补发一条
`CursorSync`（`InputHooks.cpp:609~630`），位置取 `LineEditor::GetCurrentUiCursor()`，
其基准是构造时抓的 `ConsoleState` 光标（`LineEditor.cpp:140`）。所以"回显落点"
= 回显那一刻 DLL 认为的光标位置。本探针把这个位置量出来。

复现形状刻意贴近 run.py：菜单 40+ 行（超过 30 行视口 → 会滚动）、
描述行含中文（120 列下会折行）、带长串缩进空格、末尾是 `选择 > ` 不带换行。

    python child_echo_position_probe.py
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "e2e"))

from common.paths import PROJECT_ROOT, BUILD_BIN

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)

PS7_EXE = "".join(["p", "w", "s", "h", ".exe"])
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_echopos")
SCRIPT = os.path.join(WORK, "menu.py")
RESULT = os.path.join(WORK, "menu_result.txt")

# 贴近 run.py：分栏线 + 标题 + 每项两行（标题行含 pad 空格，描述行含长缩进 + 中文长句）
SCRIPT_SRC = '''# -*- coding: utf-8 -*-
import sys
out = open(r"__RESULT__", "w", encoding="utf-8")
def rec(k, v):
    out.write("{}={}\\n".format(k, v)); out.flush()

W = 120
def div(ch):
    print(ch * min(W, 118), flush=True)
def pad(s, n):
    # 按显示宽度补空格（中文按 2 列）—— 与 run.py 的 termlib.pad 同义
    w = sum(2 if ord(c) > 0x2E80 else 1 for c in s)
    return s + " " * max(0, n - w)

div("━")
print(" menu.py  |  测试套件总入口", flush=True)
print("  · 所有测试都可在你的模拟器里直接运行; q/Esc/Ctrl+C 可随时退出单个测试", flush=True)
print("  · 窗口 %dx%d  |  按键读取: 可用" % (W, 30), flush=True)
div("━")
div("━")
print(" termtest — 终端模拟器兼容性测试套件   (13 个测试)", flush=True)
print("  · 窗口 %dx%d  |  WT_SESSION=1" % (W, 30), flush=True)
print("  · 查询探测: 无响应", flush=True)
div("━")
print("", flush=True)
TESTS = [
    ("自动一致性查询", "t_query.py", "DA1/DA2、DSR、DECRQM、DECRQSS、XTGETTCAP、OSC 颜色/剪贴板、窗口尺寸、未知序列健壮性、延迟换行 (自动 PASS/FAIL)"),
    ("文本样式与颜色", "t_sgr.py", "16/256/truecolor 三种写法、全部文本属性、下划线变体、重置语义"),
    ("光标与屏幕操作", "t_screen.py", "光标移动、擦除/插入/删除、滚动区、原点模式、换行、制表位、字符集、备用屏、窗口缩放"),
    ("Unicode 宽度", "t_unicode.py", "CJK/emoji/ZWJ/组合字符宽度、框线无缝、宽字符边界与覆盖"),
    ("按键输入", "t_input.py", "逐个按键的字节流与 xterm 规范对照、DECCKM、括号粘贴、焦点事件、未请求的鼠标上报"),
    ("鼠标协议", "t_mouse.py", "1000/1002/1003/1005/1006/1015、按钮/滚轮/修饰键、边界坐标"),
    ("交互 TUI (贪吃蛇)", "t_tui.py", "备用屏 + 高频重绘 + 输入延迟 + 增量/全量渲染对比"),
    ("OSC 扩展", "t_osc.py", "标题、OSC 8 链接、OSC 52 剪贴板、OSC 7、通知、OSC 9;4 进度、OSC 133、调色板读写"),
    ("同步输出 2026", "t_sync.py", "同步刷新 vs 撕裂对照、整屏混色压力"),
    ("图像协议", "t_images.py", "sixel / kitty graphics / iTerm2 内联图, 含滚动与叠加"),
    ("扩展键盘协议", "t_keys_ext.py", "kitty 键盘协议、CSI u、modifyOtherKeys、win32-input, 三模式字节对照表"),
    ("性能与压力", "t_perf.py", "往返延迟、fps、百万行吞吐、超长行、属性风暴、滚动 (自动出数字)"),
    ("真实程序电池", "t_real.py", "vim/less/fzf/git + rich/textual/prompt_toolkit 官方 demo 逐个检验"),
]
for i, (t, f, d) in enumerate(TESTS, 1):
    print("  " + "%2d" % i + "  " + pad(t, 20) + f, flush=True)
    print("      " + d, flush=True)
print("", flush=True)
print("  · 输入: 编号 (如 1 3) / 范围 (1-5) / 名字片段 (query) / all=全部 / q=退出", flush=True)
print("", flush=True)
print("  这几项属于终端界面本身的功能, 无法用终端内程序自动测, 建议顺手人工核对:", flush=True)
for item in ("文本选择: 拖选高亮、双击选词、复制内容是否与屏幕一致",
             "粘贴: Ctrl+V / 右键粘贴、多行粘贴是否完整、括号粘贴是否被 2004 包裹",
             "滚轮与滚动条: 滚轮翻页、滚动条位置、回滚到顶部后新输出是否正常",
             "窗口: 缩放时内容重排、失焦/获焦边框高亮、Ctrl+滚轮 调整显示大小",
             "触摸 (移动端): 单指滑动滚动、双指缩放、长按选择"):
    print("    - " + item, flush=True)

rec("READY", "1")
line = input(" 选择 > ")
rec("INPUT_RESULT", repr(line))
print("GOT=" + repr(line), flush=True)
rec("DONE", "1")
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


def render(data, cols=120, rows=30):
    import pyte
    text = data.decode("utf-8", "replace")
    text = re.sub(r"\x1b[P_X^][^\x1b]*(?:\x1b\\)?", "", text, flags=re.S)
    text = re.sub(r"\x1b\[<u", "", text)
    screen = pyte.Screen(cols, rows)
    pyte.Stream(screen).feed("\x1b[20h" + text)
    return [ln.rstrip() for ln in screen.display], (screen.cursor.x + 1, screen.cursor.y + 1)


def _case(inject, pywezterm):

    try:
        os.remove(RESULT)
    except OSError:
        pass

    pty = d_pty = None
    target_pid = 0
    try:
        pty = pywezterm.Pty(120, 30)
        target_pid, _ = pty.spawn([PS7_EXE, "-NoLogo"])
        _drain(pty, 3.5)

        if inject:
            d_pty = pywezterm.Pty(120, 30)
            d_pty.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])
            from common.paths import ti_log_path
            mlog = ti_log_path(target_pid)
            t0 = time.time()
            while time.time() - t0 < 25 and "Handshake OK" not in _read(mlog):
                time.sleep(0.2)
            _drain(d_pty, 1.0)
            term = d_pty
            label = "注入子进程"
        else:
            term = pty
            label = "原生子进程"

        term.write(list(('python "%s"\r' % SCRIPT).encode("utf-8")))
        out = _drain(term, 14.0)
        ready = b"choice" in out or b"\xe9\x80\x89\xe6\x8b\xa9" in out

        term.write(list(b"2"))
        time.sleep(0.8)
        out2 = _drain(term, 3.0)
        lines, cur = render(out + out2)

        # 找 "2" 落在哪一行、以及提示符 `选择 >` 在哪一行
        # 打印提示符整行 + '2' 所在列（区分"字符位置"与"光标位置"）
        for i, ln in enumerate(lines):
            if "选择 >" in ln:
                col = ln.find("2")
                print("      提示符行 {!r}  → '2' 在第 {} 列（0-based {}），行长 {}".format(
                    ln, (col + 1) if col >= 0 else "无", col, len(ln)))
        hit = [i + 1 for i, ln in enumerate(lines) if ln.strip().startswith("2 ")]
        prompt = [i + 1 for i, ln in enumerate(lines) if "选择 >" in ln]
        print("  [{}] 提示符就绪={} 提示符在第 {} 行；回显后光标=({},{})".format(
            label, ready, prompt or "?", cur[0], cur[1]))
        for i in hit:
            print("      第 {} 行 = {!r}".format(i, lines[i - 1][:100]))
        if not hit:
            print("      画面里没找到以 '2 ' 开头的行（可能落在别处）")
        print("      结果文件: {!r}".format(_read(RESULT).replace("\n", " ")))
        return lines, hit, prompt
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
        f.write(SCRIPT_SRC.replace("__RESULT__", RESULT))

    print("=" * 78)
    print("子进程行编辑回显落点（原生对照 vs 注入）")
    print("=" * 78)
    _case(False, pywezterm)
    _case(True, pywezterm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
