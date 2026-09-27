"""屏幕差分诊断：把"注入后的屏幕"与"原生 ConPTY 下的同一场景"逐行对比。

思路（用户提的"最终诊断"）：注入本应是**透明**的 —— 同一个程序、同样的输入、
同样的尺寸，走不走 mediator，屏幕上应该长成一样。于是不必为每个用例手写期望值，
只要拿**原生腿**当基准，和**注入腿**比。

    腿 A（基准）  pywezterm.Pty ─ cmd.exe ← 直接写输入字节            （不经 mediator）
    腿 B（被测）  pywezterm.Pty ─ mediator ─ 目标 cmd（经典 ConHost）  （正常注入链路）

两条腿跑**同一个场景脚本**（Scenario），各自把输出喂给 Terminal，最后比：

    lines（逐行文本） · cursor · scrollback_count · is_alt_screen

## 三条硬规矩（都是实测踩出来的）

1. **两条腿的启动上下文必须一致**。原生腿也要经 cmd 壳（而不是直接把目标程序
   当 Pty 的子进程），否则命令行回显/初始光标不同 → cursor 必然不等，比出来是噪声。
2. **等"安静"再比，不要固定 sleep**。用 `wait_stable(quiet=0.6)`：连续 0.6s 没有
   新字节才算画完。实测踩过：resize 后 sleep 4.5s 就开始比，慢的一侧还没重绘 →
   报出"目标没跟上新尺寸"的假差异，而 mediator/DLL 日志显示链路完全正常。
3. **先做基准自检**：同一条腿跑两遍必须一致（`self_check`）。基准自己不稳定
   （时间戳、随机数、定时器动画）的用例**不该用差分**，该老实用定点断言。

## 适用边界

差分能替代"屏幕长什么样"的等价性断言；**不能**替代：
  - 机制类事实（conptyHosted / Adopt: pid= / peerIsRelay）—— 注入侧独有，原生腿没有对手
  - 目标自检结果文件 —— 那是"程序内部的账"（DLL 虚拟状态）
  - 注入侧**故意**偏离原生的行为（例如 ConPTY 托管目标不做行首覆盖）—— 这种用例
    差分必然"不等"，且不等才是对的

## 光标为什么默认可以比、但 resize 场景要比另一套

`cursor` 在"终端自己 reflow"与"程序自己定光标"这两种语义下**归属不同**：

| 场景 | 光标归谁 | 实测 |
|---|---|---|
| 无 reflow（普通输入输出） | 两边一致 | echo / 300 行场景 cursor 完全一致 |
| 发生 reflow（缩尺寸）**且程序不设光标** | 原生归**终端**（reflow 重排它）；注入归**目标程序**（DLL 同步它） | 原生 (8,24) vs 注入 (5,0)，**文本与 scrollback 都一样** |

第二种情况**不是缺陷**：注入架构里光标本来就以目标进程为准（程序才是权威），
原生腿那个 (8,24) 是终端"替"程序算出来的。所以这类场景别拿原生光标当基准，
正确的对照物是**目标程序自己的 `GetConsoleScreenBufferInfo` 光标**。
`compare(check_cursor=False)` + 单独断"注入光标 == 目标自检光标"才是对的写法。
"""
import difflib
import os
import re
import time

from . import paths
from .session import PwSession, parse_key, _terminate_tree

COMSPEC = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")


class Screen:
    """一次屏幕快照（可比字段）。"""

    def __init__(self, lines, cursor=None, scrollback=0, alt=False, text=""):
        self.lines = list(lines)
        self.cursor = tuple(cursor) if cursor else None
        self.scrollback = int(scrollback)
        self.alt = bool(alt)
        self.text = text

    def __repr__(self):
        return "Screen(lines={}, cursor={}, sb={}, alt={})".format(
            len(self.lines), self.cursor, self.scrollback, self.alt)


def capture(leg) -> Screen:
    """从一条腿（PwSession 或 NativeLeg）取屏幕快照。"""
    return Screen(leg.lines(), leg.cursor(), leg.scrollback_count(), leg.is_alt_screen())


def norm_lines(lines, ignore=()):
    """归一化：去行尾空白 + 按正则屏蔽不稳定内容（时间戳/随机数/耗时等）。"""
    out = []
    for line in lines:
        s = line.rstrip()
        for pat in ignore:
            s = re.sub(pat, "<MASK>", s)
        out.append(s)
    return out


def compare(a: Screen, b: Screen, ignore=(), check_cursor=True) -> list:
    """比较两个快照，返回人类可读的差异列表（空 = 一致）。"""
    diffs = []
    la = norm_lines(a.lines, ignore)
    lb = norm_lines(b.lines, ignore)
    if la != lb:
        d = list(difflib.unified_diff(la, lb, "基准(原生)", "被测(注入)", lineterm="", n=1))
        diffs.append("屏幕文本不一致（基准 {} 行 / 被测 {} 行）:\n{}".format(
            len(la), len(lb), "\n".join("      " + x[:150] for x in d[:40])))
    if a.scrollback != b.scrollback:
        diffs.append("scrollback 不一致：基准 {} / 被测 {}".format(a.scrollback, b.scrollback))
    if a.alt != b.alt:
        diffs.append("alt screen 状态不一致：基准 {} / 被测 {}".format(a.alt, b.alt))
    if check_cursor and a.cursor != b.cursor:
        diffs.append("光标不一致：基准 {} / 被测 {}".format(a.cursor, b.cursor))
    return diffs


class NativeLeg:
    """原生腿：Pty + Terminal + cmd（不经 mediator）。

    接口与 PwSession 对齐，同一个场景脚本可在两条腿上跑。
    """

    def __init__(self, cols=120, rows=30, cwd=None, shell=None, scrollback=2000):
        from . import pyterm
        self._pw = pyterm.load()
        self.cols, self.rows = cols, rows
        self.cwd = cwd or paths.PROJECT_ROOT
        self.shell = shell or [COMSPEC]
        self.scrollback = scrollback
        self.pty = None
        self.term = None
        self.pid = 0
        self.buf = b""

    def start(self) -> "NativeLeg":
        self.pty = self._pw.Pty(self.cols, self.rows)
        self.term = self._pw.Terminal(self.cols, self.rows, scrollback=self.scrollback)
        self.pid, _ = self.pty.spawn(self.shell, cwd=self.cwd)
        self.wait_stable()
        return self

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- 输入 ----
    def write(self, data, lane="dst"):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.pty.write(list(data))

    def send_line(self, text, lane="dst"):
        self.write(text + "\r")

    def press(self, key, mods=0, lane="dst"):
        name, parsed = parse_key(key)
        self.write(self.term.key_down(name, mods | parsed))
        self.write(self.term.key_up(name, mods | parsed))

    # ---- 尺寸 ----
    def resize(self, cols, rows, lane="dst", order="term_first"):
        """与 PwSession.resize 同序（先模型后 pty），保证两条腿可比。"""
        cols, rows = int(cols), int(rows)
        if order == "term_first":
            self.term.resize(cols, rows)
            self.pty.resize(cols, rows)
        else:
            self.pty.resize(cols, rows)
            self.term.resize(cols, rows)
        self.cols, self.rows = cols, rows

    # ---- 输出 ----
    def wait_stable(self, quiet=0.6, timeout=8.0, lane="dst"):
        end = time.time() + timeout
        last_change = time.time()
        while time.time() < end:
            chunk = bytes(self.pty.read(65536, timeout=0.1))
            if chunk:
                self.buf += chunk
                self.term.feed(chunk)
                resp = bytes(self.term.drain_written())
                if resp:
                    self.pty.write(resp)
                last_change = time.time()
            elif time.time() - last_change >= quiet:
                break
        return 0

    def text(self, lane="dst"):
        return self.term.text()

    def lines(self, lane="dst"):
        return self.term.text().splitlines()

    def cursor(self, lane="dst"):
        return self.term.cursor()

    def scrollback_count(self, lane="dst"):
        return self.term.scrollback_count()

    def is_alt_screen(self, lane="dst"):
        return self.term.is_alt_screen_active()

    def wait_line(self, needle, timeout=10.0, lane="dst", exact=True, interval=0.2):
        deadline = time.time() + timeout
        while True:
            ls = self.lines()
            ok = any(l.strip() == needle for l in ls) if exact else any(needle in l for l in ls)
            if ok:
                return True
            if time.time() >= deadline:
                return False
            self.wait_stable(quiet=0.2, timeout=0.3)

    def wait_bytes(self, needle, timeout=10.0, lane="dst", interval=0.2):
        """等累计字节里出现 needle（比屏幕更早可见）。"""
        if isinstance(needle, str):
            needle = needle.encode("utf-8")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.buf:
                return True
            time.sleep(interval)
            self.wait_stable(quiet=0.05, timeout=interval)
        return needle in self.buf

    def close(self):
        try:
            if self.pty:
                self.pty.close()
        except Exception:
            pass
        _terminate_tree(self.pid)


def drive(leg, steps):
    """按场景脚本驱动一条腿。步骤：(kind, payload[, timeout])。

    kind: line(写一行) / raw(写原始字节) / key(按键) / resize((cols,rows)) /
          wait_line(等某行整行出现) / wait_sub(等子串) / wait_bytes(等字节) / sleep(秒)
    """
    for step in steps:
        kind = step[0]
        if kind == "line":
            leg.send_line(step[1])
        elif kind == "raw":
            leg.write(step[1])
        elif kind == "key":
            leg.press(step[1])
        elif kind == "resize":
            leg.resize(*step[1])
        elif kind == "wait_line":
            timeout = step[2] if len(step) > 2 else 10.0
            if not leg.wait_line(step[1], timeout=timeout):
                raise AssertionError("场景步骤失败：等不到行 {!r}".format(step[1]))
        elif kind == "wait_sub":
            timeout = step[2] if len(step) > 2 else 10.0
            if not leg.wait_line(step[1], timeout=timeout, exact=False):
                raise AssertionError("场景步骤失败：等不到子串 {!r}".format(step[1]))
        elif kind == "wait_bytes":
            timeout = step[2] if len(step) > 2 else 10.0
            if not leg.wait_bytes(step[1], timeout=timeout):
                raise AssertionError("场景步骤失败：等不到字节 {!r}".format(step[1]))
        elif kind == "wait_file":
            # (path, needle, timeout)：等某个文件里出现 needle ——
            # 用来等"慢的那一侧确实完成重绘"，替代固定 sleep（否则会出假差异）。
            # path 传 "@LOG" 时取环境变量 TI_DIFF_LOG 的当前值：
            # 场景脚本在两条腿上跑同一份，日志路径必须**运行时**解析，
            # 这样每条腿能用各自的文件、而敲进去的命令行仍逐字节相同。
            path, needle = step[1], step[2]
            if path == "@LOG":
                path = os.environ.get("TI_DIFF_LOG", "")
            timeout = step[3] if len(step) > 3 else 15.0
            deadline = time.time() + timeout
            ok = False
            while time.time() < deadline:
                try:
                    with open(path, "r", encoding="utf-8", errors="ignore") as f:
                        if needle in f.read():
                            ok = True
                            break
                except OSError:
                    pass
                time.sleep(0.2)
            if not ok:
                raise AssertionError(
                    "场景步骤失败：{} 内未出现 {!r}".format(path, needle))
        elif kind == "sleep":
            time.sleep(step[1])
        elif kind == "stable":
            # (quiet?) 等输出彻底安静。**每个阶段边界都该有一步**：
            # 目标"写日志/写结果文件"通常发生在画屏之前，日志一到就进入下一步，
            # 画屏字节可能还在路上 ⇒ 下一步（尤其 resize）作用在对不上的屏幕状态上，
            # 同一场景两次跑出不同结果（实测：scrollback 32 / 0）。
            leg.wait_stable(quiet=step[1] if len(step) > 1 else 0.8)
        else:
            raise ValueError("未知步骤类型 {!r}".format(kind))
    leg.wait_stable()


def run_injected(steps, cols=120, rows=30, cwd=None, host="conhost", session=None,
                 on_error=None):
    """跑注入腿。传 session 则复用（同一会话跑多场景），否则自建。

    on_error: 场景步骤失败时的取证回调 `on_error(session)`，
    用来在关闭会话前把 DLL/mediator 日志里的 resize 证据打出来（否则关掉就没了）。
    """
    own = session is None
    s = session or PwSession(cols=cols, rows=rows, host=host, cwd=cwd)
    if own:
        s.__enter__()
    try:
        drive(s, steps)
        return capture(s)
    except AssertionError:
        if on_error is not None:
            try:
                on_error(s)
            except Exception:
                pass
        raise
    finally:
        if own:
            s.__exit__(None, None, None)


def run_native(steps, cols=120, rows=30, cwd=None):
    """跑原生基准腿。"""
    with NativeLeg(cols=cols, rows=rows, cwd=cwd) as leg:
        drive(leg, steps)
        return capture(leg)


def self_check(steps, cols=120, rows=30, cwd=None, ignore=()) -> list:
    """基准自检：原生腿跑两遍必须一致。返回差异列表（空 = 基准稳定，可用差分）。"""
    a = run_native(steps, cols=cols, rows=rows, cwd=cwd)
    b = run_native(steps, cols=cols, rows=rows, cwd=cwd)
    return compare(a, b, ignore=ignore)


def dump_screens(a: Screen, b: Screen, path: str) -> None:
    """把两侧屏幕落盘留证（诊断用）。"""
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 基准（原生 ConPTY）\n")
        for i, l in enumerate(a.lines):
            f.write("{:3d}| {}\n".format(i, l))
        f.write("\n# 被测（注入）\n")
        for i, l in enumerate(b.lines):
            f.write("{:3d}| {}\n".format(i, l))
