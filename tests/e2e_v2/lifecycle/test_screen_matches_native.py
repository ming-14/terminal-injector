"""特性: 注入后屏幕 == 原生 ConPTY 屏幕（差分诊断）    类别: lifecycle

思路（用户提的"最终诊断"）: 注入应当是**透明**的。同一个程序、同样的输入、同样的
尺寸，走不走 mediator，屏幕上应该长成一样。于是不必为每个用例手写期望值 ——
拿**原生腿**当基准，和**注入腿**比：

    基准腿: pywezterm.Pty ─ cmd.exe ← 直接写输入字节（不经 mediator）
    被测腿: pywezterm.Pty ─ mediator ─ 目标 cmd（经典 ConHost，窗口隐藏）

两条腿跑**同一个场景脚本**（见 pwcommon/diff.py）。比较 lines / cursor /
scrollback / alt-screen 四项。

三个场景（覆盖不同 I/O 形态）:
  1. echo 一行              —— 基础往返：输入 → 执行 → 输出
  2. 满屏长输出 300 行      —— 滚动、回滚计数、批量输出
  3. 自绘全屏矩阵 + resize  —— WriteConsoleOutputW 整屏渲染 + 尺寸变化后重绘
                               （BUG-012 叠画 / BUG-008 scrollback 的同类场景）

先做**基准自检**：原生腿跑两遍必须一致（不稳定就不能用差分，见 diff.py 的边界说明）。

失败时把两侧屏幕落盘到 _results/ 便于对比留证。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pwcommon import pyterm
from pwcommon import paths
from pwcommon import diff
from pwcommon import target as target_mod

NAME = "screen_matches_native"
COLS, ROWS = 120, 30


def _leg_log(tag: str) -> str:
    """每条腿一个日志文件（路径经环境变量传给目标脚本）。"""
    return os.path.join(paths.RESULTS_DIR, "_diff_paint_{}.log".format(tag))


def _set_leg(tag: str) -> str:
    path = _leg_log(tag)
    os.environ["TI_DIFF_LOG"] = path
    try:
        os.remove(path)
    except OSError:
        pass
    return path

# 自绘全屏的目标脚本：**独立脚本（不带 TARGET_PREAMBLE）**，这样两条腿敲进去的
# 命令行逐字节相同 —— 命令回显本身也会出现在屏幕上，命令行不同就会造成假差异。
PAINT_SRC = r'''# -*- coding: utf-8 -*-
"""整屏自绘矩阵 + resize 重绘（差分场景 3 的目标，自动生成勿手改）。"""
import ctypes
import os
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32")

class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]

class CHAR_INFO(ctypes.Structure):
    _fields_ = [("Char", wintypes.WCHAR), ("Attributes", wintypes.WORD)]

class CSBI(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]

class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("_pad", wintypes.WORD * 10)]

STD_INPUT, STD_OUTPUT = -10, -11
k32.GetStdHandle.argtypes = [wintypes.DWORD]
k32.GetStdHandle.restype = wintypes.HANDLE
k32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
k32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.GetConsoleScreenBufferInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(CSBI)]
k32.WriteConsoleOutputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(CHAR_INFO),
                                    COORD, COORD, ctypes.POINTER(SMALL_RECT)]
k32.ReadConsoleInputW.argtypes = [wintypes.HANDLE, ctypes.POINTER(INPUT_RECORD),
                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

h_in = k32.GetStdHandle(STD_INPUT)
h_out = k32.GetStdHandle(STD_OUTPUT)
mode = wintypes.DWORD(0)
k32.GetConsoleMode(h_in, ctypes.byref(mode))
k32.SetConsoleMode(h_in, mode.value | 0x0008 | 0x0080)   # WINDOW_INPUT | EXTENDED_FLAGS

HERE = os.path.dirname(os.path.abspath(__file__))
# 日志路径走**环境变量**：每条腿用各自的文件，避免上一条腿残留的进程写进同一条日志
# 造成误判；同时又不用把路径塞进命令行 —— 两条腿敲进去的命令行必须逐字节相同。
LOG = os.environ.get("TI_DIFF_LOG") or os.path.join(HERE, "..", "_results", "_diff_paint.log")

def log(msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")

def paint():
    info = CSBI()
    k32.GetConsoleScreenBufferInfo(h_out, ctypes.byref(info))
    w = info.dwSize.X
    h = info.srWindow.Bottom - info.srWindow.Top + 1
    # 记下"程序自己认知的光标"：resize 场景里这才是光标的正确对照物
    # （原生终端会在 reflow 时自己重排光标，与"光标以目标进程为准"的注入语义不同）
    log("LAYOUT {}x{} CURSOR {},{}".format(w, h,
        info.dwCursorPosition.X, info.dwCursorPosition.Y))
    cells = (CHAR_INFO * (w * h))()
    for y in range(h):
        for x in range(w):
            ch = " "
            if x == 0:
                ch = "#"
            elif x == w - 1:
                ch = "|"
            elif x == w // 2:
                ch = "|"
            cells[y * w + x].Char = ch
            cells[y * w + x].Attributes = 0x07
    rect = SMALL_RECT(0, 0, w - 1, h - 1)
    k32.WriteConsoleOutputW(h_out, cells, COORD(w, h), COORD(0, 0), ctypes.byref(rect))

log("RUN_START")
paint()
log("FIRST_FRAME_DONE")
# 阻塞读单条输入记录；收到 WINDOW_BUFFER_SIZE_EVENT 就按新尺寸重绘。
# 注意：不能用 ReadConsoleInputW(h, NULL, 0, &n) 去"探事件数" —— 该调用会失败，
# 循环永远拿不到事件（实测踩过：脚本只在首帧画一次，resize 后不重绘）。
buf = INPUT_RECORD()
got = wintypes.DWORD(0)
t0 = time.time()
while time.time() - t0 < 60:
    if not k32.ReadConsoleInputW(h_in, ctypes.byref(buf), 1, ctypes.byref(got)):
        time.sleep(0.05)
        continue
    if got.value and buf.EventType == 0x0004:      # WINDOW_BUFFER_SIZE_EVENT
        paint()
'''


def _write_paint_script() -> str:
    paths.ensure_dirs()
    path = os.path.join(paths.TARGETS_DIR, "diff_paint.py")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(PAINT_SRC)
    return path


def _read_log() -> list:
    """读当前腿的日志（路径由 _set_leg 放进 TI_DIFF_LOG）。"""
    path = os.environ.get("TI_DIFF_LOG", "")
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return [l.strip() for l in f if l.strip()]
    except OSError:
        return []


def _evidence(session, tag: str) -> None:
    """场景步骤失败时的取证：日志里 resize 链路走到哪一环。"""
    print("         [取证-{}] 目标日志: {}".format(
        tag, [l for l in _read_log() if l.startswith("LAYOUT")]))
    dll = session.dll_log()
    hits = [l for l in dll.splitlines()
            if any(k in l for k in ("Resize", "ApplyWt", "BufferSize"))]
    print("         [取证-{}] DLL resize 行 {} 条: {}".format(
        tag, len(hits), [l[l.find("]") + 1:].strip()[:90] for l in hits[-3:]]))
    med = session.mediator_log()
    mh = [l for l in med.splitlines()
          if any(k in l for k in ("Resize", "resize", "BufferSize"))]
    print("         [取证-{}] mediator resize 行 {} 条: {}".format(
        tag, len(mh), [l.strip()[:90] for l in mh[-3:]]))


def _last_run(lines) -> list:
    """取日志里"最后一次运行"的片段（脚本每次启动写 RUN_START）。"""
    runs = []
    cur = None
    for l in lines:
        if l == "RUN_START":
            cur = []
            runs.append(cur)
        elif cur is not None:
            cur.append(l)
    return runs[-1] if runs else lines


def _sizes(block) -> list:
    """块内 LAYOUT 的尺寸序列，去掉连续重复（重绘次数不是断言对象）。"""
    out = []
    for l in block:
        if l.startswith("LAYOUT "):
            size = l.split()[1]
            if not out or out[-1] != size:
                out.append(size)
    return out


def _last_cursor(block):
    """块内最后一次 LAYOUT 行记录的"程序自认知光标" (x, y)。"""
    for l in reversed(block):
        if l.startswith("LAYOUT ") and "CURSOR " in l:
            xy = l.split("CURSOR ")[1]
            x, y = xy.split(",")
            return (int(y), int(x))   # 转成 Terminal.cursor() 的 (row, col)
    return None


def run() -> int:
    pyterm.require()
    failures = 0

    paint = _write_paint_script()
    py = sys.executable

    scenarios = [
        {
            "name": "echo 一行",
            "steps": [("line", "echo TI_DIFF_A"), ("wait_line", "TI_DIFF_A", 10.0)],
        },
        {
            "name": "满屏长输出 300 行",
            "steps": [("line", "for /l %i in (1,1,300) do @echo LINE%i"),
                      ("wait_line", "LINE300", 25.0)],
        },
        {
            "name": "自绘全屏矩阵 + resize",
            "steps": [("line", '"{}" "{}"'.format(py, paint)),
                      ("wait_file", "@LOG", "FIRST_FRAME_DONE", 20.0),
                      ("stable", 0.8),          # 首帧字节落定后再 resize，否则 reflow 对象不确定
                      ("resize", (72, 18)),
                      ("wait_file", "@LOG", "LAYOUT 72x18", 20.0),
                      ("stable", 0.8)],
            # resize + 程序不设光标 ⇒ 光标归属不同（原生归终端 reflow，注入归目标进程），
            # 所以这里不比原生光标，改用"注入光标 == 目标自检光标"（见 diff.py 文档）
            "check_cursor": False,
            "expect_sizes": ["120x30", "72x18"],
            "cursor_vs_selfcheck": True,
        },
    ]

    for sc in scenarios:
        name, steps = sc["name"], sc["steps"]
        check_cursor = sc.get("check_cursor", True)
        print("\n── 场景：{} ──".format(name))
        try:
            # 1. 基准自检：原生腿跑两遍必须一致（否则这个场景不能用差分）
            _set_leg("nat1")
            a1 = diff.run_native(steps, cols=COLS, rows=ROWS)
            _set_leg("nat2")
            a2 = diff.run_native(steps, cols=COLS, rows=ROWS)
            same = diff.compare(a1, a2, check_cursor=check_cursor)
            if same:
                print("  [FAIL] 基准不稳定（原生跑两遍就不一致），本场景不适用差分:")
                for d in same:
                    print("         " + d.replace("\n", "\n         ")[:400])
                failures += 1
                continue
            print("  [PASS] 基准稳定（原生两遍一致，{} 行）".format(len(a1.lines)))
            nat_block = _last_run(_read_log())

            # 2. 注入腿跑同一场景，与基准比
            _set_leg("inj")
            b = diff.run_injected(steps, cols=COLS, rows=ROWS,
                                  on_error=lambda sess: _evidence(sess, "注入腿"))
            inj_block = _last_run(_read_log())

            diffs = diff.compare(a1, b, check_cursor=check_cursor)
            if diffs:
                print("  [FAIL] 注入后屏幕与原生基准不一致:")
                for d in diffs:
                    print("         " + d.replace("\n", "\n         ")[:1200])
                shot = os.path.join(paths.RESULTS_DIR, "diff_{}.txt".format(NAME))
                diff.dump_screens(a1, b, shot)
                print("         （两侧屏幕已落盘: {}）".format(shot))
                failures += 1
            else:
                print("  [PASS] 注入后屏幕与原生基准逐行一致"
                      "（cursor={} sb={} alt={}）".format(b.cursor, b.scrollback, b.alt))

            # 3. 程序自己认知的尺寸序列也要一致（顺序 + 去连续重复）
            ns, is_ = _sizes(nat_block), _sizes(inj_block)
            if ns == is_:
                print("  [PASS] 目标读到的尺寸序列一致: {}".format(is_))
            else:
                print("  [FAIL] 尺寸序列不一致：原生 {} vs 注入 {}".format(ns, is_))
                failures += 1
            if sc.get("expect_sizes") and is_ != sc["expect_sizes"]:
                print("  [FAIL] 尺寸序列与期望不符：{} != {}".format(is_, sc["expect_sizes"]))
                failures += 1

            # 4. resize 场景：光标改与"目标自检的 GCSBI 光标"对齐
            if sc.get("cursor_vs_selfcheck"):
                want = _last_cursor(inj_block)
                got = (b.cursor[0], b.cursor[1]) if b.cursor else None
                if want is None:
                    print("  [FAIL] 目标未上报自检光标")
                    failures += 1
                elif got == want:
                    print("  [PASS] 注入终端光标 == 目标自检光标 {}（目标为准）".format(want))
                else:
                    print("  [FAIL] 注入终端光标 {} != 目标自检光标 {}".format(got, want))
                    failures += 1
        except AssertionError as e:
            print("  [FAIL] {}".format(e))
            failures += 1
        except (RuntimeError, KeyError) as e:
            print("  [FAIL] setup 失败: {}".format(e))
            failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
