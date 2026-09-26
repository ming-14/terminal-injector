# -*- coding: utf-8 -*-
"""termlib.py — termtest 终端兼容性测试套件公共库

被 termtest/ 目录下所有测试程序共用, 提供:
  1. 终端初始化: Windows 下开启 VT 输出/输入处理、切换 UTF-8 代码页; 统一以 UTF-8 字节写 stdout
  2. 转义序列常量与构造器 (光标 / 擦除 / 样式 / 颜色 / OSC / 标题 / 超链接)
  3. 原始按键读取: 无阻塞轮询 + 超时收集, 一次调用返回一个完整按键或粘贴块 (含按键名解析);
     被拆成好几次发来的序列会挂起重拼 (read_chunk + PARTIAL_WAIT), 分帧切分见 split_input/InputReader
  4. 终端查询: 发送查询序列并读取回包 (用于自动 PASS/FAIL 断言)
  5. 测试输出框架: 横幅 / 分节 / 提示 / 判定行 / 汇总表 / 报告落盘
  6. 非交互模式: 环境变量 TERMTEST_NONINTERACTIVE=1 时所有等待自动跳过, 便于冒烟自检
  7. 退出保护: atexit 自动恢复终端状态 (光标可见、样式复位、退出备用屏、模式复位)

使用约定:
    import termlib as T
    def main():
        T.start("t_xxx.py", ["覆盖点1", "覆盖点2"], note="说明")
        ...
        T.finish()
    if __name__ == "__main__":
        import sys; sys.exit(T.guard(main))
"""

import atexit
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
from datetime import datetime

IS_WIN = (os.name == "nt")
NONINTERACTIVE = os.environ.get("TERMTEST_NONINTERACTIVE", "") not in ("", "0")
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 转义序列常量
ESC = "\x1b"
CSI = ESC + "["
OSC = ESC + "]"
DCS = ESC + "P"
APC = ESC + "_"
PM = ESC + "^"
SOS = ESC + "X"
ST = ESC + "\\"
BEL = "\x07"

# 判定状态
PASS, FAIL, SKIP, DIFF, WARN = "PASS", "FAIL", "SKIP", "DIFF", "WARN"
_STATUS_COLOR = {PASS: "32", FAIL: "31", SKIP: "90", DIFF: "36", WARN: "33"}

INPUT_OK = False          # 是否能进入原始模式并向终端要按键
_eof = False              # stdin 已到 EOF (管道关闭/重定向到文件)
_pend = b""               # 尾部没读完整的序列 (光杆 ESC / 半截 CSI / 半个 UTF-8 字), 等下一批字节拼全
_pend_since = None        # _pend 第一次出现的时间, 用来决定还等不等
_interrupt = ""           # 最近一次"提前结束"的原因 (wait_key 系列填写, guard 在恢复终端之后打出来)
try:
    # 半截序列最多等这么久 (见 read_chunk), 超时按原样交出。
    # 调大 = 更耐"终端把一条序列分好几次发", 代价是单独按 Esc 的响应变慢;
    # 走管道/远程中继时可用环境变量覆盖 (例如 TERMTEST_PARTIAL_WAIT=0.3), 0 = 不重拼。
    PARTIAL_WAIT = max(0.0, float(os.environ.get("TERMTEST_PARTIAL_WAIT", "0.15")))
except ValueError:
    PARTIAL_WAIT = 0.15
_term = {"name": "", "covers": [], "note": "", "t0": 0.0, "rows": []}


# ---------------------------------------------------------------- 底层输出
_stdout = getattr(sys.stdout, "buffer", sys.stdout)


def write(s, flush=True):
    """以 UTF-8 字节写 stdout (绕过文本层的编码/换行转换, 保证转义序列精确)。"""
    if isinstance(s, str):
        s = s.encode("utf-8", "replace")
    try:
        _stdout.write(s)
        if flush:
            _stdout.flush()
    except Exception:
        pass


def wln(s=""):
    write(s + "\n")


# ---------------------------------------------------------------- Windows 控制台初始化
def _init_windows():
    """开启 stdout 的 VT 处理、切换代码页到 UTF-8, 否则转义序列会被当普通文本打印。"""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleOutputCP(65001)
        k.SetConsoleCP(65001)
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            # ENABLE_PROCESSED_OUTPUT(0x1) | ENABLE_VIRTUAL_TERMINAL_PROCESSING(0x4)
            k.SetConsoleMode(h, mode.value | 0x0001 | 0x0004)
    except Exception:
        pass


if IS_WIN:
    _init_windows()


def can_read_input():
    """判断 stdin 是否可读 (控制台 tty 或可轮询的管道)。"""
    try:
        if IS_WIN:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-10)
            mode = ctypes.c_uint()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                return True
            return not sys.stdin.isatty()   # 管道: 仍可 PeekNamedPipe 读
        return os.isatty(0)
    except Exception:
        return False


def input_alive():
    """输入通道是否还活着 (未到 EOF)。测试循环可用它提前跳出。"""
    return not _eof


# ---------------------------------------------------------------- 终端尺寸
def _win_console_size():
    try:
        import ctypes
        from ctypes import wintypes

        class COORD(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        class SMALL_RECT(ctypes.Structure):
            _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                        ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

        class CSBI(ctypes.Structure):
            _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                        ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                        ("dwMaximumWindowSize", COORD)]

        k = ctypes.windll.kernel32
        info = CSBI()
        if k.GetConsoleScreenBufferInfo(k.GetStdHandle(-11), ctypes.byref(info)):
            return (info.srWindow.Right - info.srWindow.Left + 1,
                    info.srWindow.Bottom - info.srWindow.Top + 1)
    except Exception:
        pass
    return None


def term_size():
    """返回 (列数, 行数)。"""
    try:
        sz = shutil.get_terminal_size()
        if sz.columns > 0 and sz.lines > 0 and (sz.columns, sz.lines) != (80, 24):
            return (sz.columns, sz.lines)
    except Exception:
        pass
    if IS_WIN:
        r = _win_console_size()
        if r:
            return r
    try:
        return (int(os.environ.get("COLUMNS", 80)), int(os.environ.get("LINES", 24)))
    except Exception:
        return (80, 24)


def dwidth(s):
    """按显示宽度计算字符串宽度 (东亚宽/全角算 2 列, 组合字符算 0)。近似实现, 仅用于排版。"""
    w = 0
    for ch in s:
        if unicodedata.combining(ch) or unicodedata.category(ch) in ("Cf", "Mn", "Me"):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def pad(s, width, align="left"):
    """按显示宽度对齐填充。"""
    gap = max(0, width - dwidth(s))
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


# ---------------------------------------------------------------- 样式/光标/屏幕
def sgr(*codes):
    return CSI + ";".join(str(c) for c in codes) + "m"


def fg(n):
    return CSI + "38;5;%dm" % n


def bg(n):
    return CSI + "48;5;%dm" % n


def fg_rgb(r, g, b):
    return CSI + "38;2;%d;%d;%dm" % (r, g, b)


def bg_rgb(r, g, b):
    return CSI + "48;2;%d;%d;%dm" % (r, g, b)


def cup(row, col):
    return CSI + "%d;%dH" % (row, col)


def put(row, col, text):
    """返回"定位到 (row,col) 并写入 text"的序列串 —— 需要调用方 write() 才会输出。"""
    return cup(row, col) + text


def cuu(n=1):
    return CSI + "%dA" % n


def cud(n=1):
    return CSI + "%dB" % n


def cuf(n=1):
    return CSI + "%dC" % n


def cub(n=1):
    return CSI + "%dD" % n


def cha(n=1):
    return CSI + "%dG" % n


def vpa(n=1):
    return CSI + "%dd" % n


def ed(n=0):
    return CSI + "%dJ" % n


def el(n=0):
    return CSI + "%dK" % n


def clear():
    """清屏并回左上角 (不擦滚动缓冲)。"""
    write(CSI + "2J" + CSI + "H")


def clear_all():
    """清屏 + 擦除滚动缓冲。"""
    write(CSI + "3J" + CSI + "2J" + CSI + "H")


def clear_below():
    """擦除光标以下到屏幕末尾 (光标不动, 不产生滚动)。

    用于清掉光标下方残留的旧画面: 否则随后打印的短行会盖在旧的长行上,
    旧行尾部的字符会从右边露出来, 看起来像"输出叠在一起"。
    """
    write(CSI + "0J")


def park_cursor(row=None):
    """把光标停到某一行并清掉该行: 之后继续追加输出就不会压到已有内容。

    绝对定位类测试 (用 CUP 挪过光标) 之后必须调用, 否则后续 T.row/T.wln 会
    从光标所在的行开始就地覆盖, 造成"短行盖长行、旧行尾巴露出来"的叠字画面。

    row 省略即末行。若屏幕上方还有必须保持可见的内容 (例如残留检测区),
    就传一个给后面若干行留出余量的值: 在末行收尾会触发滚动, 把那些内容推走。
    """
    _cols, rows = term_size()
    target = rows if row is None else row
    write(sgr(0) + cup(max(1, min(target, rows)), 1) + el(2))


def save_cursor():
    """保存光标位置与属性 (DECSC, ESC 7)。"""
    write(ESC + "7")


def restore_cursor():
    """恢复 save_cursor() 保存的位置与属性 (DECRC, ESC 8)。"""
    write(ESC + "8")


class SavedCursor(object):
    """with T.SavedCursor(): ... —— 进入时保存光标, 退出时恢复。

    用于探测型测试: 需要把光标 CUP 到别处发查询, 但探测结束后必须回到原来的
    输出位置继续顺序追加。少了这一步, 后续输出会落在被 CUP 到的那个位置, 而
    原位置到那里之间的行从未写过 —— 屏幕上就留下一大段空白。
    (park_cursor() 也能救回追加位置, 但它把光标甩到某一行、只适合本来就在
    底部收尾的场景; 探测型测试要的是"原样回到原处"。)
    """

    def __enter__(self):
        reset_style()
        save_cursor()
        return self

    def __exit__(self, *exc):
        reset_style()
        restore_cursor()
        return False


def reset_style():
    write(sgr(0))


def alt_on():
    """进入备用屏 (1049), 并按多数终端习惯清屏。"""
    write(CSI + "?1049h" + CSI + "H" + CSI + "2J")


def alt_off():
    write(CSI + "?1049l")


def show_cursor(on=True):
    write(CSI + ("?25h" if on else "?25l"))


def cursor_style(n):
    """DECSCUSR: 0 默认 / 1 闪烁块 / 2 稳定块 / 3 闪烁下划线 / 4 稳定下划线 / 5 闪烁竖线 / 6 稳定竖线。"""
    write(CSI + "%d q" % n)


def set_title(t):
    write(OSC + "2;" + t + ST)


def link(url, text=None):
    """OSC 8 超链接包裹。"""
    text = text if text is not None else url
    return OSC + "8;;" + url + ST + text + OSC + "8;;" + ST


def progress_osc(state, pct):
    """OSC 9;4 任务栏进度: state 0=清除 1=正常 2=错误 3=不确定 4=暂停。"""
    return OSC + "9;4;%d;%d" % (state, pct) + ST


def beep():
    write(BEL)


# ---------------------------------------------------------------- 排版助手
def divider(title=None, ch="─"):
    cols = max(20, term_size()[0]) - 1
    if title:
        t = " " + title + " "
        left = max(0, (cols - dwidth(t)) // 2)
        line = ch * left + t + ch * max(0, cols - left - dwidth(t))
    else:
        line = ch * cols
    wln(sgr(90) + line + sgr(0))


def section(title):
    """小节标题, 用于自报测试点。"""
    cols = term_size()[0]
    head = "▌ " + title
    fill = "─" * max(0, cols - dwidth(head) - 2)
    write("\n" + sgr(1, 36) + head + sgr(0) + sgr(90) + fill + sgr(0) + "\n")


def hint(text):
    write(sgr(90) + "  · " + text + sgr(0) + "\n")


def note(text):
    write(sgr(33) + "  ! " + text + sgr(0) + "\n")


# ---------------------------------------------------------------- 原始输入
if IS_WIN:
    _INPUT_REC = None

    def _window_input_record():
        """惰性构建 INPUT_RECORD 及配套结构 (只建一次, 轮询时不再反复造 ctypes 类)。"""
        global _INPUT_REC
        if _INPUT_REC is None:
            import ctypes
            from ctypes import wintypes

            class COORD(ctypes.Structure):
                _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

            class KEY_EVENT_RECORD(ctypes.Structure):
                _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                            ("wVirtualKeyCode", wintypes.WORD),
                            ("wVirtualScanCode", wintypes.WORD),
                            ("uChar", ctypes.c_wchar),
                            ("dwControlKeyState", wintypes.DWORD)]

            class MOUSE_EVENT_RECORD(ctypes.Structure):
                _fields_ = [("dwMousePosition", COORD), ("dwButtonState", wintypes.DWORD),
                            ("dwControlKeyState", wintypes.DWORD),
                            ("dwEventFlags", wintypes.DWORD)]

            class WINDOW_BUFFER_SIZE_RECORD(ctypes.Structure):
                _fields_ = [("dwSize", COORD)]

            class MENU_EVENT_RECORD(ctypes.Structure):
                _fields_ = [("dwCommandId", wintypes.UINT)]

            class FOCUS_EVENT_RECORD(ctypes.Structure):
                _fields_ = [("bSetFocus", wintypes.BOOL)]

            class _EVENT(ctypes.Union):
                _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("MouseEvent", MOUSE_EVENT_RECORD),
                            ("WindowBufferSizeEvent", WINDOW_BUFFER_SIZE_RECORD),
                            ("MenuEvent", MENU_EVENT_RECORD),
                            ("FocusEvent", FOCUS_EVENT_RECORD)]

            class INPUT_RECORD(ctypes.Structure):
                _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]

            _INPUT_REC = INPUT_RECORD
        return _INPUT_REC

    # UnicodeChar 为 0、但在 VT 输入下仍产出 CSI 序列的键: 导航键 + 功能键。
    # 其余 UnicodeChar 为 0 的键 (Shift/Ctrl/Alt/CapsLock 等) 单独按下不产出任何字节。
    _VK_BYTE_KEYS = frozenset([0x0C, 0x21, 0x22, 0x23, 0x24, 0x25,
                               0x26, 0x27, 0x28, 0x2D, 0x2E] + list(range(0x70, 0x88)))

    def _win_console_has_key(h):
        """控制台输入队列里是否存在"能产出字节"的按键按下记录。

        不能拿 WaitForSingleObject 的返回值当"可读"用: 窗口尺寸变化、鼠标点击、
        按键抬起这几类记录同样会把句柄置为有信号, 而它们不产出任何字节 ——
        此时 os.read() 会一直阻塞到下一次真正的按键 (实测: 启动时切一次控制台模式,
        队列里就会多出一条窗口尺寸记录, 足以让每一次查询都读不出来)。
        终端回答的查询回包在控制台里同样是按键按下记录, 因此不会漏判。
        只查看不消费: 记录仍交给 os.read() 按它自己的规则处理。
        """
        import ctypes
        k = ctypes.windll.kernel32
        n = ctypes.c_ulong(0)
        if not k.GetNumberOfConsoleInputEvents(h, ctypes.byref(n)):
            return True                     # 取不到队列长度: 退回旧语义, 由读自己处理
        if n.value == 0:
            return False
        limit = 128
        rec_t = _window_input_record()
        buf = (rec_t * limit)()
        got = ctypes.c_ulong(0)
        if not k.PeekConsoleInputW(h, buf, limit, ctypes.byref(got)):
            return True
        for i in range(got.value):
            rec = buf[i]
            if rec.EventType != 1:          # 只认 KEY_EVENT
                continue
            ke = rec.Event.KeyEvent
            if not ke.bKeyDown:             # 按键抬起: 不产出字节
                continue
            if ord(ke.uChar or "\x00") != 0 or ke.wVirtualKeyCode in _VK_BYTE_KEYS:
                return True
        return False

    def _win_console_wait(h, seconds):
        """在控制台句柄上等待"有按键可读", 超时返回 False (超时是真的超时)。"""
        import ctypes
        k = ctypes.windll.kernel32
        deadline = time.monotonic() + seconds
        while True:
            if _win_console_has_key(h):
                return True
            remain = deadline - time.monotonic()
            if remain <= 0:
                return False
            # 用 WaitForSingleObject 当"睡"用: 有新记录立刻醒, 醒来再判断它是否能产出字节
            k.WaitForSingleObject(h, max(1, int(min(remain, 0.02) * 1000)))


def _wait_readable(seconds):
    """等待 stdin 可读。返回 True 表示现在可以读 (此时读下去不会阻塞)。"""
    seconds = max(0.0, seconds)
    if IS_WIN:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-10)
        mode = ctypes.c_uint()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            return _win_console_wait(h, seconds)
        avail = ctypes.c_ulong(0)
        if k.PeekNamedPipe(h, None, 0, None, ctypes.byref(avail), None):
            if avail.value > 0:
                return True
            time.sleep(min(seconds, 0.02))
            return False
        return True     # 文件/其他: 让读取自己返回 EOF
    try:
        import select
        r, _, _ = select.select([0], [], [], seconds)
        return bool(r)
    except Exception:
        time.sleep(min(seconds, 0.02))
        return True


def _read_avail():
    global _eof
    try:
        b = os.read(0, 4096)
    except OSError:
        _eof = True
        return b""
    if not b:
        _eof = True
    return b


class Raw(object):
    """with T.Raw(): ... 进入原始输入模式 (关回显/行缓冲/流控), 退出时恢复。

    Windows 下同时关闭 QuickEdit (否则鼠标点击会冻结输出) 并打开 VT 输入,
    这样方向键等会以 escape 序列原样送达。
    """

    def __enter__(self):
        global INPUT_OK
        self._saved = None
        self._h = None
        self._fd = None
        # INPUT_OK 是"这个终端能不能按键"的能力标记 (start() 里探测得到), 各测试靠它决定
        # 要不要做按键采集。一次 Raw 尝试不该永久改写它 —— query() 内部就 with Raw(),
        # 于是"查询一次"会把标记改成"按键读取不可用", 后面的段落全被误判成没法测。
        # 进入时照旧按实际结果更新 (块内逻辑可能要看), 退出时还原。
        self._input_ok = INPUT_OK
        if IS_WIN:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-10)
            mode = ctypes.c_uint()
            if k.GetConsoleMode(h, ctypes.byref(mode)):
                self._h = h
                self._saved = mode.value
                # 关: 行输入(0x2) 回显(0x4) 已处理输入(0x1) QuickEdit(0x40)
                # 开: 扩展标志(0x80) VT 输入(0x200)
                new = (mode.value & ~0x0002 & ~0x0004 & ~0x0001 & ~0x0040) | 0x0080 | 0x0200
                INPUT_OK = bool(k.SetConsoleMode(h, new))
            else:
                INPUT_OK = False
        else:
            try:
                import termios
                self._fd = sys.stdin.fileno()
                self._saved = termios.tcgetattr(self._fd)
                new = list(self._saved)
                new[0] = new[0] & ~(termios.IXON | termios.ICRNL | termios.INLCR | termios.IGNCR)
                new[3] = new[3] & ~(termios.ICANON | termios.ECHO | termios.ISIG |
                                    termios.IEXTEN | termios.ECHONL)
                new[6][termios.VMIN] = 1
                new[6][termios.VTIME] = 0
                termios.tcsetattr(self._fd, termios.TCSANOW, new)
                INPUT_OK = True
            except Exception:
                INPUT_OK = False
        return self

    def __exit__(self, *exc):
        global INPUT_OK
        INPUT_OK = self._input_ok
        try:
            if self._saved is None:
                pass
            elif IS_WIN:
                import ctypes
                ctypes.windll.kernel32.SetConsoleMode(self._h, self._saved)
            else:
                import termios
                termios.tcsetattr(self._fd, termios.TCSANOW, self._saved)
        except Exception:
            pass
        return False


def read_chunk(timeout=None, gap=0.03):
    """读取一次完整块: 等到第一个字节, 再把 gap 秒内连续到达的字节合并。

    方向键 F5 = ESC[15~ 会一次性返回 5 字节; 粘贴会合并成一个大块。
    超时且无数据返回 None。

    尾部若是**没收完整的序列** (只到 ESC、CSI 只到一半、UTF-8 只到半个字),
    不当成结果立刻交出: 先挂到 _pend, 最多再等 PARTIAL_WAIT 秒把它拼全, 这期间
    返回 None。不这么做的话, 终端把 ESC[A 分两次发过来就会被上层看成"单独按了
    Esc" —— 贪吃蛇、分页器这类程序会因此突然退出 (实测 60ms 拆开必现)。

    等够 PARTIAL_WAIT 仍不完整, 说明对面确实只发了这些 (例如用户真的只按了 Esc),
    此时按原样交出; 半截尾巴若已被挂起, 本轮返回的是"去掉尾巴"的完整部分。
    """
    global _pend, _pend_since
    if _eof:
        # EOF 后再挂着的半截序列没有补全的可能了 —— 交出去, 别把用户按的 Esc 吞掉
        out, _pend, _pend_since = _pend, b"", None
        return out or None
    t0 = time.monotonic()
    deadline = None if timeout is None else t0 + timeout
    buf = bytearray(_pend)                 # 上次挂起的半截序列先接上
    _pend = b""
    while not buf:
        if _wait_readable(0.02):
            b = _read_avail()
            if b:
                buf += b
                break
            # EOF 或读空
            if _eof:
                return None
        if deadline is not None and time.monotonic() >= deadline:
            return None
        if timeout is None and _eof:
            return None
    gap_deadline = time.monotonic() + gap
    while True:
        remain = gap_deadline - time.monotonic()
        if remain <= 0:
            break
        if _wait_readable(min(0.005, remain)):
            more = _read_avail()
            if more:
                buf += more
                gap_deadline = time.monotonic() + gap
    cut = _tail_incomplete(buf)
    if cut is None:
        _pend_since = None
    else:
        if _pend_since is None:
            _pend_since = time.monotonic()
        if _eof or time.monotonic() - _pend_since >= PARTIAL_WAIT:
            _pend_since = None             # 等够了: 原样交出, 由上层自己判
        else:
            _pend = bytes(buf[cut:])
            del buf[cut:]
            if not buf:
                return None                # 手上只有半截, 本轮什么也不给, 下次接着凑
    return bytes(buf)


def drain(timeout=0.05):
    """清空待读输入, 返回被丢弃的字节 (含 read_chunk 手里挂着的半截序列)。"""
    global _pend, _pend_since
    data = bytearray(_pend)
    _pend, _pend_since = b"", None
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        b = read_chunk(timeout=0.01, gap=0.01)
        if not b:
            break
        data += b
    return bytes(data)


# ---------------------------------------------------------------- 输入切分
# read_chunk 只保证"把一小段时间内到达的字节攒成一块", 块里可能挤着好几样东西
# (方向键 + 鼠标报文 + 粘贴), 也可能一条序列被拆到两次读里。下面只做"分帧":
# 从左到右认出每条序列的边界, 是什么交给 key_name() / mouse_event() 判。
# 不这么切的话, 任何"粘在一起"的输入都会被整块当成一条, 结果是整块作废:
# 鼠标移动的高频报文首尾相连成几百字节, re.fullmatch 必然失配。

_ESC = 0x1b
_CSI_FINAL_MIN, _CSI_FINAL_MAX = 0x40, 0x7e
_ESC_INTERMEDIATES = range(0x20, 0x30)      # ESC ( B / ESC ) 0 之类的中间字节
_STR_INTROS = (0x50, 0x58, 0x5e, 0x5f)      # DCS P / SOS X / PM ^ / APC _
_OSC_INTRO = 0x5d
_PASTE_BEGIN = b"\x1b[200~"
_PASTE_END = b"\x1b[201~"


def _char_end(data, i):
    """单个字符 (UTF-8 可能多字节) 的结束位置; 还没收完整返回 None。"""
    c = data[i]
    if c < 0x80:
        return i + 1
    if 0xc0 <= c <= 0xdf:
        need = 2
    elif 0xe0 <= c <= 0xef:
        need = 3
    elif 0xf0 <= c <= 0xf7:
        need = 4
    else:
        return i + 1                       # 非法起头字节, 当一个字节算
    tail = data[i + 1:i + need]
    if len(tail) < need - 1:
        return None                        # 续字节还没到齐
    if any(not (0x80 <= b <= 0xbf) for b in tail):
        return i + 1                       # 续字节不合法 → 不是 UTF-8, 按单字节处理
    return i + need


def _csi_end(data, i, j):
    """从 data[j] 起找 CSI/SS3 的终结字节 (0x40-0x7E)。返回结束位置或 None。"""
    n = len(data)
    while j < n:
        c = data[j]
        if c == _ESC:
            # 参数中间冒出 ESC: 说明前一条序列本身是被截断的, 到此收尾,
            # 让后面这条 ESC 序列 (很可能是个真按键) 自己去成条。
            return j
        if _CSI_FINAL_MIN <= c <= _CSI_FINAL_MAX:
            if c == 0x4d and j == i + 2:
                # X10 鼠标: ESC [ M 后面还跟 3 个原始字节, 它们可能小于 0x40,
                # 不能被当成新序列
                return j + 4 if j + 4 <= n else None
            return j + 1
        j += 1
    return None


def _unit_end(data, i):
    """从 data[i] 起算一条完整序列的结束位置 (不含); 还没收完整返回 None。"""
    n = len(data)
    if data[i] != _ESC:
        return _char_end(data, i)
    if i + 1 >= n:
        return None                        # 光一个 ESC, 由 split_input 决定怎么算
    nxt = data[i + 1]
    if nxt == 0x5b:                        # CSI: ESC [
        if data.startswith(_PASTE_BEGIN, i):
            j = data.find(_PASTE_END, i + len(_PASTE_BEGIN))
            return None if j < 0 else j + len(_PASTE_END)
        return _csi_end(data, i, i + 2)
    if nxt == 0x4f:                        # SS3: ESC O (再跟终结字节)
        return _csi_end(data, i, i + 2)
    if nxt == _OSC_INTRO or nxt in _STR_INTROS:
        j = i + 2                          # OSC/DCS/SOS/PM/APC: 直到 BEL 或 ST
        while j < n:
            if data[j] == 0x07:
                return j + 1
            if data[j] == _ESC and j + 1 < n and data[j + 1] == 0x5c:
                return j + 2
            j += 1
        return None
    if nxt in _ESC_INTERMEDIATES:
        return i + 3 if i + 3 <= n else None    # ESC ( B 之类
    return i + 2                           # ESC + 单字节 (Alt+键 / 单字符)


def _tail_incomplete(buf):
    """buf 尾部是不是"还没收完整的一条序列"? 是则返回它的起始下标, 否则 None。

    用 _unit_end 从左到右走一遍, 走不动的位置就是半截序列的开头。
    光杆 ESC 也算没收完整: 它既可能是"用户只按了 Esc", 也可能是某条序列的开头,
    只能靠时间区分 —— read_chunk 用 PARTIAL_WAIT 等一段时间再决定。
    """
    i, n = 0, len(buf)
    while i < n:
        if buf[i] == _ESC and i + 1 >= n:
            return i
        end = _unit_end(buf, i)
        if end is None:
            return i
        i = end
    return None


def split_input(data, pending=b""):
    """把字节块按序切成一条条完整序列。返回 (units, rest)。

    units — [bytes, ...], 每条是 CSI / SS3 / OSC / DCS / ESC+单或双字节 / 单个字符;
            括号粘贴整段 (ESC[200~ … ESC[201~) 作为一条给出, 内部不再切分
            (粘贴内容里出现 ESC 也不能被当成按键)。
    rest  — 尾部没收完整的一段 (CSI 只到一半、粘贴还没出现结束标记…),
            调用方应把它并入下一块再切; 收尾时若 rest 仍有内容, 说明对面发的
            东西本身不完整 (例如只送了 ESC[200~ 却没有 ESC[201~)。

    注意: 走到这里还看见"光杆 ESC"时, read_chunk 已经为它等过 PARTIAL_WAIT,
    所以它算一条完整的"Esc 键" —— 上游的 read_chunk 不会再让它半路被交出来。
    """
    buf = pending + data
    units, i, n = [], 0, len(buf)
    while i < n:
        if buf[i] == _ESC and i + 1 >= n:
            # 光杆 ESC: 按"单独按 Esc"算 (read_chunk 已等过 PARTIAL_WAIT 仍没有后续字节)。
            # 不能留成残片, 否则 Esc 键与 q/Esc 退出都收不到。
            units.append(buf[i:i + 1])
            i += 1
            break
        end = _unit_end(buf, i)
        if end is None:
            break
        units.append(buf[i:end])
        i = end
    return units, buf[i:]


class InputReader(object):
    """按序切分输入, 并跨块保留没读完整的尾巴。

        r = T.InputReader()
        for u in r.read(timeout=0.3):
            ...            # 每个 u 是一条完整序列, 可以逐条判定
    """

    def __init__(self, gap=0.03):
        self._buf = b""
        self._gap = gap

    def read(self, timeout=0.3, gap=None):
        """读一次输入。返回本次收到的完整序列列表 (可能为空)。"""
        chunk = read_chunk(timeout=timeout, gap=self._gap if gap is None else gap)
        if not chunk:
            return []
        self._buf += chunk
        units, self._buf = split_input(self._buf)
        return units

    def discard(self):
        """丢弃半截残留 (放弃本次输入时用), 返回被丢掉的字节。"""
        b, self._buf = self._buf, b""
        return b

    @property
    def incomplete(self):
        """当前还压在缓冲里、没凑成完整序列的尾巴。"""
        return self._buf


class QuitTest(Exception):
    """用户在测试中主动退出 (q / Esc / Ctrl+C)。"""


def wait_key(hint_text="按任意键继续  (q / Esc / Ctrl+C 退出)"):
    """等待任意键。非交互模式下直接返回 b''。返回按键字节。"""
    if NONINTERACTIVE:
        write("\r" + el(2))      # 回到行首并清行: 否则提示会接在调用方那半截行后面
        hint("非交互模式: 跳过等待")
        return b""
    hint(hint_text)
    return wait_key_silent()


def wait_key_silent():
    """只等待一个按键, 不打印任何提示。

    用于"绝对定位画面"类的测试 (如滚动区/原点模式): 打印提示会产生换行而滚动屏幕,
    把刚画好的参考标记推走。需要提示时请自己用 cup+el 在固定行写, 再调用本函数。
    """
    global _interrupt
    if NONINTERACTIVE:
        write("\r" + el(2))      # 回到行首并清行: 调用方常把提示写在半截行上
        hint("非交互模式: 跳过等待")
        return b""
    inp = InputReader()
    while True:
        units = inp.read(timeout=None)
        if not units:
            if _eof:
                # 原因交给 guard 在恢复终端之后再打: 这里可能正在备用屏里, 打了也会被 1049l 擦掉
                _interrupt = "stdin 已到 EOF, 无法继续交互"
                raise QuitTest()
            continue
        for u in units:
            if u in (b"q", b"\x1b", b"\x03") or u[:1] == b"\x03":
                _interrupt = "收到退出键 %s" % key_name(u)
                raise QuitTest()
            ev = mouse_event(u)
            if ev is not None:
                # 鼠标报文不是"任意键" (否则鼠标一动就把等待跳过去了), 但要记账:
                # 本套件从不请求鼠标上报, 收到就是终端多送的。
                noise_mouse(u, ev)
                continue
            return u


def ask_yn(question):
    """向用户提一个 y/n 问题。返回 True/False/None(无回答)。"""
    if NONINTERACTIVE:
        hint(question + "  → 非交互模式: 记 SKIP")
        return None
    k = wait_key(question + "   [y=是 / n=否]")
    c = k[:1].lower()
    if c == b"y":
        return True
    if c == b"n":
        return False
    return None


def wait_resize(old=(0, 0), timeout=30.0):
    """等待窗口尺寸变化, 返回新尺寸或 None(超时/非交互)。"""
    if NONINTERACTIVE:
        write("\r" + el(2))      # 回到行首并清行: 调用方常把提示写在半截行上
        hint("非交互模式: 跳过尺寸变化等待")
        return None
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        sz = term_size()
        if sz != tuple(old):
            return sz
        time.sleep(0.1)
    return None


# ---------------------------------------------------------------- 按键名称解析
_MODS = {1: "", 2: "Shift", 3: "Alt", 4: "Shift+Alt", 5: "Ctrl",
         6: "Shift+Ctrl", 7: "Alt+Ctrl", 8: "Shift+Alt+Ctrl"}

_EXACT = {}


def _reg(seq, name):
    _EXACT[seq] = name


_reg(b"\x1b[A", "Up 上"); _reg(b"\x1b[B", "Down 下")
_reg(b"\x1b[C", "Right 右"); _reg(b"\x1b[D", "Left 左")
_reg(b"\x1bOA", "Up 上(DECCKM 应用态)"); _reg(b"\x1bOB", "Down 下(DECCKM 应用态)")
_reg(b"\x1bOC", "Right 右(DECCKM 应用态)"); _reg(b"\x1bOD", "Left 左(DECCKM 应用态)")
_reg(b"\x1b[H", "Home"); _reg(b"\x1bOH", "Home(SS3)"); _reg(b"\x1b[1~", "Home(CSI 1~)")
_reg(b"\x1b[F", "End"); _reg(b"\x1bOF", "End(SS3)"); _reg(b"\x1b[4~", "End(CSI 4~)")
_reg(b"\x1b[2~", "Insert"); _reg(b"\x1b[3~", "Delete")
_reg(b"\x1b[5~", "PageUp"); _reg(b"\x1b[6~", "PageDown")
_reg(b"\x1b[Z", "Shift+Tab")
_reg(b"\x1bOP", "F1"); _reg(b"\x1bOQ", "F2"); _reg(b"\x1bOR", "F3"); _reg(b"\x1bOS", "F4")
for _code, _name in ((15, "F5"), (17, "F6"), (18, "F7"), (19, "F8"), (20, "F9"),
                     (21, "F10"), (23, "F11"), (24, "F12")):
    _reg(("\x1b[%d~" % _code).encode(), _name)
_reg(b"\x1b[200~", "括号粘贴: 开始 (bracketed paste begin)")
_reg(b"\x1b[201~", "括号粘贴: 结束 (bracketed paste end)")
_reg(b"\x1b[I", "焦点进入 (focus in)"); _reg(b"\x1b[O", "焦点离开 (focus out)")
_reg(b"\x1b[2;2~", "Shift+Insert"); _reg(b"\x1b[2;3~", "Alt+Insert")
_reg(b"\x1b[3;2~", "Shift+Delete"); _reg(b"\x1b[5;2~", "Shift+PageUp")
_reg(b"\x1b[6;2~", "Shift+PageDown")
_reg(b"\x1b[1;5C", "Ctrl+Right"); _reg(b"\x1b[1;5D", "Ctrl+Left")
_reg(b"\x1b[1;2C", "Shift+Right"); _reg(b"\x1b[1;2D", "Shift+Left")

_FKEY_CSI = {2: "Insert", 3: "Delete", 5: "PageUp", 6: "PageDown", 15: "F5", 17: "F6",
             18: "F7", 19: "F8", 20: "F9", 21: "F10", 23: "F11", 24: "F12"}

_MOUSE_BTN = {0: "左键", 1: "中键", 2: "右键"}


def mouse_event(data):
    """解析鼠标事件字节流。返回 dict 或 None。

    支持 X10 (ESC [ M + 3 字节)、UTF-8(1005)、SGR(1006, ESC [ < b;x;y M/m)。
    """
    if isinstance(data, str):
        data = data.encode("latin-1", "replace")
    s = data
    if s.startswith(b"\x1b[<"):
        m = re.fullmatch(rb"\x1b\[<(\d+);(\d+);(\d+)([Mm])", s)
        if not m:
            return None
        return _mk_mouse(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                         m.group(4) == b"M", "SGR(1006)")
    if s.startswith(b"\x1b[M") and len(s) > 3:
        rest = s[3:]
        # 含高位字节 → 可能是 UTF-8(1005) 坐标
        if any(c >= 0x80 for c in rest):
            try:
                chars = rest.decode("utf-8")
            except Exception:
                chars = ""
            if len(chars) >= 3:
                vals = [ord(c) for c in chars[:3]]
                return _mk_mouse(vals[0] - 32, vals[1] - 32, vals[2] - 32, None, "UTF-8(1005)")
        if len(rest) >= 3:
            return _mk_mouse(rest[0] - 32, rest[1] - 32, rest[2] - 32, None, "X10(默认)")
    return None


def _mk_mouse(b, x, y, pressed, enc):
    mods = []
    if b & 4:
        mods.append("Shift")
    if b & 8:
        mods.append("Alt")
    if b & 16:
        mods.append("Ctrl")
    motion = bool(b & 32)
    base = b & 0xC3
    if base in _MOUSE_BTN:
        name = _MOUSE_BTN[base]
    elif base == 3:
        name = "无按键" if motion else "释放"
    elif base == 64:
        name = "滚轮上"
    elif base == 65:
        name = "滚轮下"
    elif base == 66:
        name = "滚轮左"
    elif base == 67:
        name = "滚轮右"
    else:
        name = "未知按键字段"
    if 64 <= base <= 67:
        action = "滚轮"
    elif motion:
        # 移动类事件按"有没有按住按钮"分: 按住=拖拽, 没按住=移动。
        # 不能沿用 按下/释放 —— 那会拼出 "无按键移动 按下" 这种自相矛盾的说法。
        action = "拖拽" if base in _MOUSE_BTN else "移动"
    elif base == 3 or pressed is None:
        # b=3 不带移动位 = 释放但终端没报是哪个按钮; X10/UTF-8 编码不报按下/释放。
        action = "事件"
    else:
        action = "按下" if pressed else "释放"
    return {"raw": b, "base": base, "name": name, "action": action,
            "x": x, "y": y, "mods": mods, "enc": enc, "motion": motion}


def mouse_text(ev, pos=True):
    """鼠标事件的可读描述。pos=False 时省略坐标 (用于"未被请求的上报"记账落盘:
    坐标是用户的手部轨迹, 报告只需要事件类型)。"""
    mods = ("+" + "+".join(ev["mods"])) if ev.get("mods") else ""
    if pos:
        return "%s %s @ (%d, %d)%s  [%s b=%d]" % (
            ev["name"], ev["action"], ev["x"], ev["y"], mods, ev.get("enc", ""), ev.get("raw", -1))
    return "%s %s%s  [%s]" % (ev["name"], ev["action"], mods, ev.get("enc", ""))


def key_name(data):
    """把按键字节翻译成人话 (含转义序列), 便于逐项排查。

    传进来的是"一块"而不是"一条"时, 先按序切开再逐条命名 —— 否则几条粘在一起
    (鼠标移动报文尤其容易) 会整块失配、只能报一句"未知序列"。
    """
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    if not data:
        return "(空)"
    if len(data) > 1:
        units, rest = split_input(data)
        if len(units) != 1 or rest:
            parts = [key_name(u) for u in units[:3]]
            if rest:
                if rest.startswith(_PASTE_BEGIN) and not rest.endswith(_PASTE_END):
                    parts.append("括号粘贴: 只有开始标记 (内容 %d 字节, 没有 ESC[201~)"
                                 % (len(rest) - len(_PASTE_BEGIN)))
                else:
                    parts.append("(半截 %d 字节)" % len(rest))
            if len(units) > 3:
                parts.append("…另 %d 条" % (len(units) - 3))
            return " + ".join(parts) if parts else "(半截 %d 字节)" % len(rest)
    if data in _EXACT:
        return _EXACT[data]
    if data.startswith(_PASTE_BEGIN):
        if data.endswith(_PASTE_END):
            return "括号粘贴 (包裹完整, 内容 %d 字节)" % (
                len(data) - len(_PASTE_BEGIN) - len(_PASTE_END))
        return "括号粘贴: 只有开始标记 (内容 %d 字节, 没有 ESC[201~)" % (
            len(data) - len(_PASTE_BEGIN))
    if len(data) == 1:
        c = data[0]
        if c == 0x1b:
            return "Esc (0x1B)"
        if c == 0x0d:
            return "Enter (CR 0x0D)"
        if c == 0x0a:
            return "Ctrl+J / LF (0x0A)"
        if c == 0x09:
            return "Tab / Ctrl+I (0x09)"
        if c == 0x08:
            return "Ctrl+H / 退格 (0x08)"
        if c == 0x7f:
            return "Backspace (DEL 0x7F)"
        if c == 0x00:
            return "Ctrl+@ / NUL (0x00)"
        if 0x1c <= c <= 0x1f:
            return "Ctrl+%s (0x%02X)" % ("\\]^_"[c - 0x1c], c)
        if 1 <= c <= 26:
            return "Ctrl+%s (0x%02X)" % (chr(64 + c), c)
        if 32 <= c < 127:
            return "'%s' (0x%02X)" % (chr(c), c)
        return "单字节 0x%02X" % c

    s = data.decode("latin-1")
    ev = mouse_event(data)
    if ev:
        return "鼠标: " + mouse_text(ev)
    m = re.fullmatch(r"\x1b\[1;(\d)([ABCDHF])", s)
    if m:
        mod = _MODS.get(int(m.group(1)), "mod=%s" % m.group(1))
        base = {"A": "Up 上", "B": "Down 下", "C": "Right 右", "D": "Left 左",
                "H": "Home", "F": "End"}[m.group(2)]
        return ("%s+" % mod) + base
    m = re.fullmatch(r"\x1b\[(\d+);(\d)~", s)
    if m:
        code, mod = int(m.group(1)), int(m.group(2))
        base = _FKEY_CSI.get(code, "%d~" % code)
        return ("%s+" % _MODS.get(mod, "mod=%s" % mod)) + base
    m = re.fullmatch(r"\x1bO1;(\d)([PQRS])", s)
    if m:
        base = {"P": "F1", "Q": "F2", "R": "F3", "S": "F4"}[m.group(2)]
        return ("%s+" % _MODS.get(int(m.group(1)), "mod=%s")) + base
    m = re.fullmatch(r"\x1b\[(\d+);(\d+)(?::(\d))?u", s)
    if m:
        return "kitty 键盘: code=%s mods=%s%s" % (
            m.group(1), m.group(2), (" 事件=%s" % m.group(3)) if m.group(3) else "")
    m = re.fullmatch(r"\x1b\[27;(\d+);(\d+)~", s)
    if m:
        return "modifyOtherKeys: code=%s mods=%s" % (m.group(2), m.group(1))
    m = re.fullmatch(r"\x1b\[(\d+);(\d+);(\d+);(\d+);(\d+);(\d+)_", s)
    if m:
        return "win32-input: VK=%s Sc=%s Uc=%s Kd=%s Cs=%s Rc=%s" % m.groups()
    if len(data) == 2 and data[0] == 0x1b:
        try:
            return "Alt+" + data[1:].decode("utf-8")
        except Exception:
            return "Alt+0x%02X" % data[1]
    if len(data) > 2 and data[0] == 0x1b and not s.startswith("\x1b["):
        try:
            return "Alt+" + data[1:].decode("utf-8")
        except Exception:
            pass
    return "未知序列 (%d 字节): %s" % (len(data), vis(data))


def vis(data):
    """把字节流转成可读形式: 控制字符写成 ESC/CR/LF/^X, 其余原样。"""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    if not data:
        return "(空)"
    try:
        s = data.decode("utf-8")
        as_utf8 = True
    except UnicodeDecodeError:
        s = data.decode("latin-1")
        as_utf8 = False
    out = []
    for ch in s:
        o = ord(ch)
        if o == 0x1b:
            out.append("ESC")
        elif o == 0x07:
            out.append("BEL")
        elif o == 0x0d:
            out.append("CR")
        elif o == 0x0a:
            out.append("LF")
        elif o == 0x09:
            out.append("TAB")
        elif o < 0x20:
            out.append("^" + chr(o + 64))
        elif o == 0x7f:
            out.append("^?")
        elif o >= 0x80 and not as_utf8:
            out.append("\\x%02x" % o)
        else:
            out.append(ch)
    return "".join(out)


def hexdump(data):
    """返回形如 '1B 5B 41' 的十六进制串。"""
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    return " ".join("%02X" % c for c in data)


def dump_bytes(data):
    """组合: 十六进制 + 可读形式。"""
    return "%s   | %s" % (hexdump(data), vis(data))


# ---------------------------------------------------------------- 未被请求的上报
# 本套件测的是"终端有没有按规范办事": 程序没请求鼠标上报, 终端就不该送鼠标报文。
# 这类无请求上报不只是一屏噪声 —— 它会和按键粘在同一块里, 把按键判定、查询回包、
# 性能数字一起带偏。所以统一在这里记账 (谁收到的都记: query / wait_key / 逐条判定),
# 收尾时由 finish() 落一行判定, 免得噪声被悄悄吞掉。
_noise = {"mouse": 0, "bytes": 0, "first": None, "last": None,
          "kinds": {}, "encs": {}, "noted": False, "rowed": False}


def noise_mouse(unit, ev=None):
    """记一条"没请求却收到"的鼠标报文, 返回解析出的事件 (解析不出则 None)。"""
    _noise["mouse"] += 1
    _noise["bytes"] += len(unit)
    if ev is None:
        ev = mouse_event(unit)
    if ev is None:
        return None
    k = "%s %s" % (ev["name"], ev["action"])
    _noise["kinds"][k] = _noise["kinds"].get(k, 0) + 1
    enc = ev.get("enc", "?")
    _noise["encs"][enc] = _noise["encs"].get(enc, 0) + 1
    if _noise["first"] is None:
        _noise["first"] = mouse_text(ev, pos=False)
    _noise["last"] = mouse_text(ev, pos=False)
    return ev


def noise_stats():
    """返回本次运行记下的"未被请求的上报"计数 (供报告用)。"""
    return dict(_noise, kinds=dict(_noise["kinds"]), encs=dict(_noise["encs"]))


def noise_row(name="鼠标上报未被请求", quiet=False):
    """把"未被请求的鼠标上报"落成一条判定。

    quiet=True (给 finish() 用) 时只在真有上报时落行 —— 规规矩矩的终端不必多一行 PASS。
    同一次运行只落一行: 测试自己先落了 (如 t_input), finish() 就不重复。
    """
    if _noise["rowed"]:
        return
    if not _noise["mouse"]:
        if quiet:
            return
        _noise["rowed"] = True
        row(name, PASS, "未请求期间没有收到鼠标报文")
        return
    _noise["rowed"] = True
    top = sorted(_noise["kinds"].items(), key=lambda kv: -kv[1])[:3]
    detail = "共 %d 条/%d 字节 [%s%s] 首条 %s" % (
        _noise["mouse"], _noise["bytes"],
        " / ".join("%s×%d" % kv for kv in top),
        " 等" if len(_noise["kinds"]) > 3 else "",
        _noise["first"] or "(解析不出)")
    row(name, DIFF, detail + " — 本程序从不开启鼠标上报, 且开篇/每节开始都先发过一遍关闭序列, "
                            "终端不应上报")


def strip_mouse(data, note_it=False):
    """从字节流里剔掉鼠标报文, 返回剩下的字节 (同时记账)。"""
    units, rest = split_input(data)
    kept = []
    for u in units:
        if noise_mouse(u):
            continue
        kept.append(u)
    if _noise["mouse"] and note_it and not _noise["noted"]:
        _noise["noted"] = True
        note("收到未被请求的鼠标报文 %d 条 (共 %d 字节) — 终端未等程序请求就上报, "
             "已从回包中剔除; 收尾会单列一行判定" % (_noise["mouse"], _noise["bytes"]))
    return b"".join(kept) + rest


# ---------------------------------------------------------------- 终端查询
def query(seq, timeout=0.35, quiet=0.08, maxlen=8192):
    """发送查询序列并读取回包。

    返回 bytes(可能为空则 None = 无响应)。发不支持的查询序列时终端应静默,
    因此 None 通常意味着"终端不实现该查询", 而不是出错。
    """
    with Raw():
        drain(0.03)
        write(seq, flush=True)
        buf = bytearray()
        t0 = time.monotonic()
        last = t0
        while time.monotonic() - t0 < timeout and len(buf) < maxlen:
            if _wait_readable(0.02):
                b = _read_avail()
                if b:
                    buf += b
                    last = time.monotonic()
                    continue
            now = time.monotonic()
            if buf and now - last >= quiet:
                # 静默够久了, 但尾部看着还没收完 (半截 CSI / DCS / OSC): 再等到 timeout 为止。
                # 不等的话, "回包被拆成两次发"就会被当成"回包就长这样", 终端没做错的事会被判 FAIL。
                if _tail_incomplete(buf) is not None and not _eof \
                        and time.monotonic() - t0 < timeout:
                    time.sleep(0.005)
                    continue
                break
            time.sleep(0.005)
    if not buf:
        return None
    data = strip_mouse(bytes(buf), note_it=True)
    return data if data else None


# ---------------------------------------------------------------- 隐私收口 (脱敏)
# 任何进入报告文件或上屏的路径与文本都可能带走开发者身份 (用户名、机器名、目录布局、
# 会话 GUID)。约定:
#   * 上屏/落盘的路径一律过 safe_path(): 项目内给相对路径, 其余只给"目录类别/文件名";
#   * 报告统一走 save_report() 落盘, 写入前过 redact() 正则脱敏 —— 全套件只有这一处
#     open(..., "w"), 在这里收口即可覆盖全部测试与全部字段;
#   * 起子进程的环境一律过 safe_env(): 剔掉携带用户名/机器名的变量;
#   * mkdtemp 出来的临时目录一律 T.tempdir_register() 注册退出清理, 不留 %TEMP% 残留。
# RFC 2606 保留域 (example.com 等) 与测试夹具值不在脱敏范围。
_REDACT_WIN_USER = re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\"'<>\s]+")
_REDACT_FILE_URI = re.compile(r"file://[^/\s]+/")
_REDACT_EMAIL = re.compile(r"\b[\w.]+@[\w.]+\.\w{2,}\b")
_REDACT_IPV4 = re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")
_REDACT_WT_SESSION = re.compile(r"(WT_SESSION=)[0-9a-fA-F-]{36}")
_REDACT_PRIVATE_KEY = re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----")
_EMAIL_OK = ("@example.com", "@example.org", "@example.net")


def redact(text):
    """报告收口脱敏: 用户路径 / 邮箱 / IPv4 / 会话 GUID / 私钥标记在落盘前统一抹除。"""
    if not text or not isinstance(text, str):
        return text
    text = _REDACT_WIN_USER.sub("<用户路径>", text)
    text = _REDACT_FILE_URI.sub("file://<主机>/", text)
    text = _REDACT_EMAIL.sub(
        lambda m: m.group(0) if m.group(0).lower().endswith(_EMAIL_OK) else "<邮箱>",
        text)
    text = _REDACT_IPV4.sub("<IP>", text)
    text = _REDACT_WT_SESSION.sub(r"\g<1>1", text)
    text = _REDACT_PRIVATE_KEY.sub("<私钥块>", text)
    return text


def safe_path(p):
    """把绝对路径转成可安全上屏/落盘的形式: 项目内给相对路径, 其余只给目录类别 + 文件名。"""
    if not p:
        return p
    try:
        rp = os.path.relpath(p, HERE)
        if not rp.startswith(".."):
            return rp
    except Exception:
        pass
    try:
        if os.path.abspath(p).startswith(os.path.abspath(tempfile.gettempdir()) + os.sep):
            return "<系统临时目录>/" + os.path.basename(p)
    except Exception:
        pass
    return "<外部路径>/" + os.path.basename(p)


_ENV_IDENTITY = frozenset((
    "USERNAME", "COMPUTERNAME", "USERDOMAIN", "USERDNSDOMAIN", "DOMAINNAME",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "LOGONSERVER", "SESSIONNAME",
))


def safe_env():
    """给子进程用的去身份化环境: 剔掉携带用户名/机器名的变量。

    PATH / TEMP / SystemRoot / TERM 等仍原样传递, 否则 git/vim/python 起不来;
    TEMP 的值本身含用户路径, 由报告侧 redact() 兜底。剔掉 APPDATA/USERPROFILE 还能
    顺带阻止子进程 (git 等) 读到开发者本人的全局配置。
    """
    env = {}
    for k, v in os.environ.items():
        if k.upper() in _ENV_IDENTITY:
            continue
        env[k] = v
    return env


def tempdir_register(path):
    """注册退出时自动删除的临时目录, 防 %TEMP% 残留。"""
    if path:
        atexit.register(shutil.rmtree, path, True)


# ---------------------------------------------------------------- 判定与报告
def rows():
    """返回已记录的判定行列表 [(name, status, detail), ...]。"""
    return list(_term["rows"])


def row(name, status, detail=""):
    """记录并打印一条判定, 同时计入汇总。"""
    _term["rows"].append((name, status, detail))
    c = _STATUS_COLOR.get(status, "0")
    label = "[%s]" % status
    line = "  " + sgr(0, c) + label + sgr(0) + " " * max(1, 7 - len(label)) + pad(name, 26)
    if detail:
        line += " " + sgr(90) + detail + sgr(0)
    wln(line)


def summary(title="判定汇总"):
    """打印判定汇总表, 返回 FAIL 数量。"""
    rows = _term["rows"]
    if not rows:
        return 0
    counts = {}
    divider(title)
    for name, status, detail in rows:
        c = _STATUS_COLOR.get(status, "0")
        label = "[%s]" % status
        line = "  " + sgr(0, c) + label + sgr(0) + " " * max(1, 7 - len(label)) + pad(name, 28)
        if detail:
            line += " " + sgr(90) + detail + sgr(0)
        wln(line)
    for _, st, _ in rows:
        counts[st] = counts.get(st, 0) + 1
    parts = []
    for st in (PASS, FAIL, WARN, DIFF, SKIP):
        if counts.get(st):
            parts.append(sgr(0, _STATUS_COLOR.get(st, "0")) + "%s %d" % (st, counts[st]) + sgr(0))
    divider()
    wln("  统计: " + "   ".join(parts))
    return counts.get(FAIL, 0)


def save_report(name, text):
    """把文本报告写入 termtest/_reports/, 返回文件路径。

    全套件唯一的报告落盘出口: 写入前先过 redact() 脱敏。文件名时间戳只到日期粒度
    (同日重跑按序号递增), 不把"几点几分在跑测试"写进文件名。
    """
    d = os.path.join(HERE, "_reports")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        return None
    base = os.path.join(d, "%s_%s" % (name, datetime.now().strftime("%Y%m%d")))
    p = base + ".txt"
    n = 2
    while os.path.exists(p):
        p = "%s_%02d.txt" % (base, n)
        n += 1
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(redact(text))
        return p
    except Exception:
        return None


# ---------------------------------------------------------------- 测试框架
def term_info():
    cols, rows = term_size()
    d = {"size": (cols, rows), "objs": []}
    # 只记录"哪些环境变量存在", 不记录值: 值 (终端名/会话 GUID/真彩标记) 是环境指纹
    for k in ("TERM", "COLORTERM", "TERM_PROGRAM", "WT_SESSION", "ConEmuANSI"):
        if os.environ.get(k):
            d["objs"].append(k)
    return d


def start(name, covers, note=""):
    """测试程序开篇: 清屏 + 打印横幅 (程序名/覆盖项/终端信息)。

    注意: 参数 note 会遮蔽同名的 note() 提示函数, 本函数内不再调用 note()。
    """
    global INPUT_OK
    INPUT_OK = can_read_input()
    _term.update(name=name, covers=list(covers), note=note,
                 t0=time.monotonic(), rows=[])
    # 进门先清空全部终端模式: 模式是"粘"的, 上一次运行/上一个测试的残留会让本次判定失真
    # (例如别处没关掉的鼠标上报, 会被本次的「未请求上报」检查算到本终端头上)。
    mode_reset()
    if not NONINTERACTIVE:
        write(CSI + "2J" + CSI + "H")
    write(sgr(0))
    cols, rows = term_size()
    divider(ch="━")
    wln(sgr(1, 37) + " " + name + sgr(0) + sgr(90) + "  |  " + "  ".join(covers) + sgr(0))
    if note:
        hint(note)
    info = term_info()
    line = "窗口 %dx%d" % info["size"]
    if info["objs"]:
        line += "  |  " + "  ".join(info["objs"])
    line += "  |  按键读取: " + ("可用" if INPUT_OK else "不可用")
    if NONINTERACTIVE:
        line += "  |  非交互模式"
    hint(line)
    divider(ch="━")


def finish(msg="测试结束"):
    noise_row(quiet=True)      # 真有"没请求却上报"时才单列一行
    fails = summary()
    divider(ch="━")
    line = "  %s  (用时 %.1f 秒)" % (msg, time.monotonic() - _term["t0"])
    if fails:
        line += sgr(0, "31") + "   未通过 %d 项" % fails + sgr(0)
    wln(line)
    hint("单独重跑: python %s   |   返回菜单: python run.py" % _term["name"])
    # 收尾: 复位可能残留的模式, 并擦掉光标以下的旧画面 ——
    # 否则随后打印的内容 (如 run.py 菜单) 会盖在旧行上露出尾巴, 看起来像"叠字"。
    write(sgr(0) + CSI + "?7h" + CSI + "4l" + CSI + "?1l" + CSI + "?6l" + CSI + "r"
          + ESC + "(B" + "\x0f" + CSI + "?25h" + CSI + "0J")


# ---------------------------------------------------------------- 模式隔离
# 终端模式 (鼠标上报 / 括号粘贴 / 焦点事件 / 同步输出 / 键盘协议…) 是"粘"的: 上一个测试
# 没关掉, 或者终端忽略了关闭序列, 下一个测试就会在脏状态下得出结论 —— 而且分不清
# "我的模式没生效"和"上一个模式的残留"。
# 约定: 每个测试程序开篇 (start) 先 mode_reset() 清一遍; 需要开模式的节用 mode_scope() 包住,
# 出节 (含异常) 再清一遍。所有关闭序列只写在这一处, 免得各测试各写一份、互相漂移。
MOUSE_MODES = (1000, 1002, 1003, 1005, 1006, 1015, 1016)

MODE_RESET_SEQ = (
    sgr(0)
    + "".join(CSI + "?%dl" % m for m in MOUSE_MODES)   # 鼠标上报全部关闭
    + CSI + "?2004l"        # 括号粘贴
    + CSI + "?1004l"        # 焦点事件
    + CSI + "?2026l"        # 同步输出
    + CSI + "?1l"           # DECCKM 应用光标键
    + CSI + "?6l"           # DECOM 原点模式
    + CSI + "?7h"           # DECAWM 自动换行 (默认开)
    + CSI + "4l"            # IRM 插入模式
    + CSI + "?25h"          # DECTCEM 光标可见
    + CSI + "r"             # 滚动区复位
    + ESC + "(B" + "\x0f"   # 字符集回 ASCII / SI
    + CSI + "<u"            # kitty 键盘协议 pop
    + CSI + ">4;0m"         # modifyOtherKeys 复位 (默认关)
    + CSI + "?9001l"        # win32-input 关闭
)
# 刻意不含备用屏 (?1049l): 中途发它可能把用户正看着的画面切走/擦掉, 只在退出时由
# restore_all() 处理; 也不含 DECCOLM (?3l) —— 那会改窗口宽度, 由 t_screen 自己收尾。


def mode_reset():
    """把本套件用到的所有终端模式复位到默认状态。"""
    write(MODE_RESET_SEQ)


def mode_on(item):
    """把 mode_scope(on=[...]) 里的一项目翻译成"打开"序列。

    `1002` / `"1002"` 这种是私有模式号; 也可以直接给完整序列 (例如键盘协议 `CSI > 1 u`)。
    """
    if isinstance(item, int) or str(item).isdigit():
        return CSI + "?%sh" % item
    return item


class mode_scope(object):
    """`with T.mode_scope(on=[1002, 1006]):` —— 一节测试的模式隔离。

    进入: 先 mode_reset() 清空全部模式, 再打开 on 里列的模式, 于是本节开始时
          终端处于"只有这些模式开着"的确定状态。
    退出: 先发 off 里的额外关闭序列, 再 mode_reset() 清空 —— 异常退出也照样清。
    """

    def __init__(self, on=(), off=()):
        self._on = list(on)
        self._off = list(off)

    def __enter__(self):
        mode_reset()
        if self._on:
            write("".join(mode_on(x) for x in self._on))
        return self

    def __exit__(self, *exc):
        if self._off:
            write("".join(self._off))
        mode_reset()
        return False


def restore_all():
    """恢复终端状态, 幂等 (guard / atexit 均会调用)。"""
    write(sgr(0)
          + CSI + "?25h"          # 光标可见
          + CSI + "0 q"           # 光标样式恢复默认
          + CSI + "r"             # 滚动区复位
          + CSI + "?6l"           # 关原点模式
          + CSI + "?7h"           # 开自动换行
          + CSI + "4l"            # 关插入模式
          + CSI + "?1l"           # 关应用光标键
          + CSI + "?1000l" + CSI + "?1002l" + CSI + "?1003l"
          + CSI + "?1005l" + CSI + "?1006l" + CSI + "?1015l" + CSI + "?1016l"
          + CSI + "?2004l"        # 关括号粘贴
          + CSI + "?1004l"        # 关焦点事件
          + CSI + "?2026l"        # 关同步输出
          + ESC + "(B" + "\x0f"   # 字符集回 ASCII / SI
          + CSI + "<u"            # kitty 键盘协议 pop (不支持则被忽略)
          + CSI + ">4;0m"         # modifyOtherKeys 复位
          + CSI + "?9001l"        # win32-input 关闭
          + CSI + "?3l"           # DECCOLM 回 80 列 (t_screen 的 DECCOLM 一节中途退出会留着 132 列)
          + CSI + "?1049l")       # 退出备用屏
    try:
        _stdout.flush()
    except Exception:
        pass


atexit.register(restore_all)


def guard(fn):
    """包装 main(): 捕获退出请求, 无论成败都恢复终端。返回退出码。"""
    global _interrupt
    code = 0
    interrupted = False
    try:
        r = fn()
        if isinstance(r, int):
            code = r
    except QuitTest:
        interrupted = True
        write("\n")
    except KeyboardInterrupt:
        interrupted = True
        _interrupt = "收到 Ctrl+C"
        write("\n")
    except Exception:
        import traceback
        write("\n")
        # 完整栈里的绝对路径 (含用户名) 是最容易被原样贴到 issue/群里的身份泄漏点:
        # 打印前把项目前缀与用户目录先替换掉
        txt = traceback.format_exc()
        txt = txt.replace(HERE, ".")
        txt = re.sub(r"[A-Za-z]:\\Users\\[^\\<>\"'\s]+", "<用户路径>", txt)
        txt = re.sub(r"[A-Za-z]:/Users/[^/<>\"'\s]+", "<用户路径>", txt)
        try:
            sys.stderr.write(txt)
        except Exception:
            pass
        code = 1
    finally:
        try:
            restore_all()
        except Exception:
            pass
    if interrupted:
        # 退出请求经常发生在备用屏里 (t_images / t_screen 会在备用屏内等按键), 那种情况下
        # 现场打的提示会被 ?1049l 一起擦掉, 用户只看到"测试忽然没了"。统一挪到恢复终端之后打,
        # 位置也正好是这次运行的最后一行; 顺手把"已经判出来的东西"汇总一下, 免得白测。
        note("提前结束: %s" % (_interrupt or "收到退出键 (q/Esc/Ctrl+C)"))
        _interrupt = ""
        summary()
    return code


def guard_selftest():
    """非交互冒烟检查入口: 打印库基础能力, 供人工核对。"""
    print("termlib: NONINTERACTIVE=%s INPUT_OK=%s size=%s" %
          (NONINTERACTIVE, INPUT_OK, term_size()))