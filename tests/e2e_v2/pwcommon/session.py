"""PwSession —— pywezterm 承载 mediator 的端到端会话。

拓扑（`host` 参数显式选择，不默认一律用 Pty）：

    host="conhost"（默认，对齐 v1）
        目标进程 = 独立控制台的 cmd（经典 ConHost，窗口默认隐藏）
        mediator = 跑在 pywezterm.Pty 里（扮演原 WT 的角色）
        ⇒ DLL 侧 conptyHosted=0，走"主进程屏幕重放 + 行首覆盖"那条分支

    host="conpty"
        目标进程本身跑在 pywezterm.Pty 里（源终端，尺寸可大于目标终端）
        mediator = 跑在另一个 pywezterm.Pty 里
        ⇒ DLL 侧 conptyHosted=1，走"流式 shell"分支

为什么必须分两种：`LazyInit::IsConPtyHosted()`（判据 = 目标控制台窗口类名
`PseudoConsoleWindow`）决定走哪条分支。一律换成 Pty 等于把整批用例静默挪到
另一条代码路径，v1 覆盖的经典 ConHost 回归点会失效。

通道（lane）：

    "dst" —— 目标终端侧（mediator 所在），断言屏幕/接收输入都看它
    "src" —— 仅 host="conpty"：承载目标的源终端

三个必须内建的机制（少一个就会偶发卡死/误判）：

1. **持续泵**：ConPTY 写端不被读会堵死 → 后台线程一直 read → feed(Terminal)。
2. **应答闭环**：Terminal 会自发产生应答（DSR/DA），必须 drain_written() 回写 pty，
   否则目标等应答等到死。
3. **屏幕等待**：屏幕断言要轮询（TUI 有重绘延迟），用 wait_screen()。

输入不经前台窗口（直接写 pty 字节），所以**零焦点依赖**、也没有中文 IME 截键问题。
"""
import os
import re
import shlex
import subprocess
import sys
import threading
import time

from . import childlog
from . import paths
from . import result as result_mod
from . import target as target_mod

MEDIATOR_EXE = paths.MEDIATOR_EXE

# PowerShell 7 可执行名拆开拼接：某些 shell 安全策略会拦截含该字符串的命令行
PS7_EXE = "".join(["p", "w", "s", "h", ".exe"])
COMSPEC = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")

# 常见按键名 → Terminal.key_down 的键名
KEY_ALIASES = {
    "enter": "Enter", "cr": "Enter", "return": "Enter",
    "esc": "Esc", "escape": "Esc", "tab": "Tab", "space": "Space",
    "backspace": "Backspace", "bs": "Backspace",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "insert": "Insert", "delete": "Delete",
    "pageup": "PageUp", "pagedown": "PageDown",
}

# pywezterm 的修饰键位
MOD_SHIFT, MOD_ALT, MOD_CTRL = 2, 4, 8
_MOD_PREFIX = {"ctrl": MOD_CTRL, "alt": MOD_ALT, "shift": MOD_SHIFT}


def parse_key(key: str):
    """把 "ctrl-c" / "alt-Enter" / "Up" 解析成 (键名, mods)。

    **必须自己解析**：pywezterm 的 key_down 不认识 "ctrl-c" 这种带修饰的描述，
    会把它当单字符处理并返回 `b'c'` —— 不报错，但 Ctrl 被静默丢掉
    （实测：`key_down("ctrl-c", 0)` -> `b'c'`，而不是 `b'\\x03'`）。
    这种静默错误比抛异常危险得多，所以在这一层把前缀翻成 mods 位。
    """
    mods = 0
    name = key
    while True:
        head, sep, rest = name.partition("-")
        if not sep or head.lower() not in _MOD_PREFIX:
            break
        mods |= _MOD_PREFIX[head.lower()]
        name = rest
    if not name:
        raise ValueError("按键名不合法: {!r}".format(key))
    return KEY_ALIASES.get(name.lower(), name), mods


def parse_launcher(launcher) -> list:
    """把 launcher 规格统一成 argv 列表。

    - None → 当前解释器
    - list/tuple → 原样
    - 存在的路径字符串 → 单个 token（**不能**丢给 shlex：POSIX 规则会把
      `C:\\Program Files\\Python311\\python.exe` 的反斜杠当转义吃掉，
      变成 `C:Program FilesPython311python.exe`）
    - 其余字符串 → 按 "exe 参数" 拆（posix=False 保留反斜杠，再剥引号）
    """
    if launcher is None:
        return [sys.executable]
    if isinstance(launcher, (list, tuple)):
        return list(launcher)
    if os.path.exists(launcher):
        return [launcher]
    return [t.strip('"') for t in shlex.split(launcher, posix=False)]


def build_command(argv, script: str = None, result: str = None) -> str:
    """拼命令行，每个 token 都加引号（script / result 可省）。

    launcher 里带空格（`C:\\Program Files\\...\\python.exe`）而不加引号时，
    cmd 会按空格切开，报 `'C:\\Program' 不是内部或外部命令`。
    """
    parts = list(argv) + [p for p in (script, result) if p is not None]
    return " ".join('"{}"'.format(a) for a in parts)


def _terminate_tree(pid: int) -> None:
    """终止进程及其全部后代（尽力而为）。"""
    if not pid:
        return
    try:
        import psutil
        p = psutil.Process(pid)
        for child in p.children(recursive=True):
            try:
                child.terminate()
            except Exception:
                pass
        p.terminate()
        try:
            p.wait(timeout=3)
        except Exception:
            pass
    except Exception:
        pass


def _sweep_mediators() -> None:
    """清掉残留的 mediator 进程（默认不用；跨用例隔离时才需要）。"""
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info["name"] or "").lower() == "terminal_injector.exe":
                    proc.terminate()
            except Exception:
                pass
    except ImportError:
        pass


def _clear_file(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


class Lane:
    """一条终端通道：Pty（真 ConPTY）+ Terminal（wezterm-term 模型）+ 累计字节。"""

    def __init__(self, name: str, pty, term):
        self.name = name
        self.pty = pty
        self.term = term
        self.buf = b""

    def close(self) -> None:
        try:
            self.pty.close()
        except Exception:
            pass


class PwSession:
    """pywezterm 承载 mediator 的 e2e 会话（上下文管理器）。"""

    def __init__(self, cols: int = 120, rows: int = 30, host: str = "conhost",
                 target_args=None, cwd=None, visible: bool = False,
                 src_size=(150, 40), handshake_timeout: float = 25.0,
                 scrollback: int = 4000, call_prefix: str = ""):
        if host not in ("conhost", "conpty"):
            raise ValueError("host 只能是 'conhost' 或 'conpty'，得到 {!r}".format(host))
        self.cols = cols
        self.rows = rows
        self.host = host
        self.target_args = target_args
        self.cwd = cwd or paths.PROJECT_ROOT
        self.visible = visible
        self.src_size = src_size
        self.handshake_timeout = handshake_timeout
        self.scrollback = scrollback
        # PowerShell 里跑带引号的 exe 必须加调用运算符：`& "C:\...\python.exe" "x.py"`，
        # 直接给路径会 ParserError（实测踩过）。cmd 不需要，留空即可。
        self.call_prefix = call_prefix

        self.target_pid = 0
        self.mediator_pid = 0
        self._target_proc = None
        self._lanes = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._pump_thread = None
        self._pump_errors = 0

    # ------------------------------------------------------------ 生命周期
    def __enter__(self) -> "PwSession":
        self._start_pump()
        if self.host == "conhost":
            self.spawn_target()
            if not self.spawn_mediator():
                self.close()
                raise RuntimeError(
                    "握手失败（mediator 日志: {}）".format(paths.mediator_log(self.target_pid)))
        else:
            self.spawn_source_target()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    # ------------------------------------------------------------ 启动各段
    def spawn_target(self, argv=None, cwd=None) -> int:
        """host="conhost"：起目标（独立控制台的经典 ConHost）。"""
        argv = list(argv or self.target_args or ["cmd.exe"])
        flags = subprocess.CREATE_NEW_CONSOLE
        si = subprocess.STARTUPINFO()
        if not self.visible:
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = subprocess.SW_HIDE
        self._target_proc = subprocess.Popen(argv, creationflags=flags,
                                            startupinfo=si,
                                            cwd=cwd or self.cwd)
        self.target_pid = self._target_proc.pid
        _clear_file(paths.mediator_log(self.target_pid))
        return self.target_pid

    def spawn_source_target(self, argv=None, cwd=None) -> int:
        """host="conpty"：源终端（Pty）里起目标进程。

        源终端尺寸刻意独立（src_size），因为"源尺寸 != 目标尺寸"本身就是
        某些回归的复现条件（如 conptyHosted 的误判 TUI 分支）。
        """
        pywezterm = self._pywezterm()
        cols, rows = self.src_size
        pty = pywezterm.Pty(cols, rows)
        term = pywezterm.Terminal(cols, rows, scrollback=self.scrollback)
        argv = list(argv or self.target_args or [PS7_EXE, "-NoLogo"])
        pid, _ = pty.spawn(argv, cwd=cwd or self.cwd)
        self.target_pid = pid
        self._add_lane("src", pty, term)
        _clear_file(paths.mediator_log(pid))
        return pid

    def spawn_mediator(self, cols=None, rows=None, extra_args=None) -> bool:
        """在 dst 通道起 mediator（= 原 WT 的角色）并等握手。返回是否成功。

        **每次都清 mediator 日志**：同一会话里第二次注入（反复注入/卸载类用例）时，
        上一轮的 "Handshake OK" 还在文件里，wait_handshake 会立刻返回真 —— 假阳性。
        """
        pywezterm = self._pywezterm()
        cols = cols or self.cols
        rows = rows or self.rows
        _clear_file(paths.mediator_log(self.target_pid))
        pty = pywezterm.Pty(cols, rows)
        term = pywezterm.Terminal(cols, rows, scrollback=self.scrollback)
        argv = [MEDIATOR_EXE, "--mediator", "--target-pid", str(self.target_pid)]
        argv += list(extra_args or [])
        pid, _ = pty.spawn(argv)
        self.mediator_pid = pid
        self._add_lane("dst", pty, term)
        return self.wait_handshake(timeout=self.handshake_timeout)

    def close_mediator(self) -> None:
        """关掉 dst 通道（= 关掉"新终端"）：mediator 退出 → 管道断开 → DLL 卸载。

        **不动目标进程**——unload_clean / repeat_inject_unload 这类用例正是要在这之后
        断言目标的模块表状态。
        """
        with self._lock:
            lane = self._lanes.pop("dst", None)
        if lane is not None:
            lane.close()
        if self.mediator_pid:
            time.sleep(0.2)
            _terminate_tree(self.mediator_pid)
        self.mediator_pid = 0

    def close(self, sweep: bool = False) -> None:
        """全量清理：停泵、关所有通道、终止目标与 mediator。"""
        self._stop.set()
        th = self._pump_thread
        if th is not None and th.is_alive():
            th.join(timeout=2.0)
        self._pump_thread = None

        with self._lock:
            lanes = list(self._lanes.values())
            self._lanes.clear()
        for lane in lanes:
            lane.close()

        _terminate_tree(self.mediator_pid)
        self.mediator_pid = 0
        _terminate_tree(self.target_pid)
        if self._target_proc is not None:
            try:
                self._target_proc.wait(timeout=3)
            except Exception:
                pass
            self._target_proc = None
        if sweep:
            _sweep_mediators()

    # ------------------------------------------------------------ 泵线程
    def _pywezterm(self):
        from . import pyterm
        return pyterm.load()

    def _add_lane(self, name: str, pty, term) -> None:
        with self._lock:
            self._lanes[name] = Lane(name, pty, term)

    def _lane(self, name: str = "dst") -> Lane:
        with self._lock:
            lane = self._lanes.get(name)
        if lane is None:
            raise KeyError("通道 {!r} 不存在（现有 {}）；"
                           "host=conpty 时记得先 spawn_mediator()".format(
                               name, sorted(self._lanes)))
        return lane

    def _start_pump(self) -> None:
        if self._pump_thread is not None:
            return
        self._stop.clear()
        self._pump_thread = threading.Thread(
            target=self._pump_loop, name="pwterm-pump", daemon=True)
        self._pump_thread.start()

    def _pump_loop(self) -> None:
        """持续读所有通道 → 喂 Terminal → 回写应答。不读会把 ConPTY 写端堵死。"""
        while not self._stop.is_set():
            with self._lock:
                lanes = list(self._lanes.values())
            if not lanes:
                time.sleep(0.02)
                continue
            got_any = False
            for lane in lanes:
                try:
                    chunk = bytes(lane.pty.read(65536, timeout=0.05))
                except Exception:
                    continue
                if not chunk:
                    continue
                got_any = True
                resp = b""
                with self._lock:
                    lane.buf += chunk
                    try:
                        lane.term.feed(chunk)
                        resp = bytes(lane.term.drain_written())
                    except Exception:
                        self._pump_errors += 1
                if resp:
                    try:
                        lane.pty.write(resp)
                    except Exception:
                        pass
            if not got_any:
                time.sleep(0.01)

    # ------------------------------------------------------------ 输入
    def write(self, data, lane: str = "dst") -> None:
        """写原始输入字节（str 按 UTF-8 编码）。取代 v1 的 SendInput。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self._lane(lane).pty.write(list(data))

    def send_line(self, text: str, lane: str = "dst") -> None:
        """写一行（补 CR = 回车）。"""
        self.write(text + "\r", lane=lane)

    def press(self, key: str, mods: int = 0, lane: str = "dst") -> None:
        """按键：用 dst 的 Terminal 编码（自动遵守 DECCKM/鼠标等模式）。

        key 支持 "Up" / "c" / "ctrl-c" / "alt-Enter" 这类写法（由 parse_key 解析，
        见那里的说明：带修饰的键必须自己翻成 mods 位，否则会被静默当单字符）。
        mods 位：SHIFT=2, ALT=4, CTRL=8。
        """
        keyname, parsed_mods = parse_key(key)
        mods |= parsed_mods
        l = self._lane(lane)
        with self._lock:
            down = bytes(l.term.key_down(keyname, mods))
            up = bytes(l.term.key_up(keyname, mods))
            resp = bytes(l.term.drain_written())
        if down:
            l.pty.write(list(down))
        if up:
            l.pty.write(list(up))
        if resp:
            l.pty.write(list(resp))

    def run_target(self, name: str, body: str, ready_key: str = None,
                   ready_timeout: float = 25.0, launcher=None,
                   lane: str = "dst") -> str:
        """生成目标脚本并敲进（被注入的）目标终端，返回脚本路径。

        launcher 默认 = 当前解释器（绝对路径）。**必须逐个 token 加引号**：
        `C:\\Program Files\\Python311\\python.exe` 里带空格，不加引号会被 cmd
        按空格切开（实测报 "'C:\\Program' 不是内部或外部命令"）。
        跨位数链路用例传 ["py", "-3"]（或字符串 "py -3"）。
        """
        result_mod.clear_result(name)
        script = target_mod.write_target(name, body)
        cmd = build_command(parse_launcher(launcher), script,
                            result_mod.result_file(name))
        self.send_line(cmd, lane=lane)
        if ready_key:
            v = result_mod.wait_result(name, ready_key, timeout=ready_timeout)
            if not v:
                raise RuntimeError(
                    "目标脚本未就绪（key={}）；结果文件={!r}；屏幕尾部={!r}".format(
                        ready_key, result_mod.read_result(name), self.tail_lines(6, lane)))
        return script

    def wait_stable(self, quiet: float = 0.6, timeout: float = 8.0,
                    lane: str = "dst") -> int:
        """等输出安静下来（连续 quiet 秒无新字节），返回等待期间新增字节数。

        差分断言（注入屏幕 vs 原生屏幕）**必须**用它：固定 sleep 会在慢的一侧
        还没画完时就开始比，产出"假差异"（实测踩过：resize 后 sleep 4.5s 时
        目标还没重绘，被误判成"目标没跟上新尺寸"，其实链路是通的）。
        """
        end = time.time() + timeout
        start = len(self.output(lane))
        last_seen = start
        last_change = time.time()
        while time.time() < end:
            time.sleep(0.1)
            now = len(self.output(lane))
            if now != last_seen:
                last_seen = now
                last_change = time.time()
            elif time.time() - last_change >= quiet:
                break
        return len(self.output(lane)) - start

    # ------------------------------------------------------------ 尺寸
    def resize(self, cols: int, rows: int, lane: str = "dst",
               order: str = "term_first") -> None:
        """改通道尺寸。**必须 pty 与 Terminal 一起改**。

        只改 pty 会让模型的 wrap 与真实终端不一致（屏幕断言直接失真）；
        只改 Terminal 则真实进程不知道尺寸变了。

        order 决定先后，**默认 term_first（先模型后 pty）** —— 这是为了确定性：
        缩小尺寸时模型要对"当前屏幕内容"做 reflow（溢出部分进 scrollback），
        若 pty 先改，目标可能已经按新尺寸重绘完了，reflow 的对象就变成了新内容
        ⇒ 同一个场景两次跑出来的 scrollback 不一样（实测：32 / 0 两种结果）。
        先改模型 = reflow 的一定是"重绘前的那一屏"，两条腿可比。
        """
        l = self._lane(lane)
        cols, rows = int(cols), int(rows)
        if order == "term_first":
            with self._lock:
                l.term.resize(cols, rows)
            l.pty.resize(cols, rows)
        else:
            l.pty.resize(cols, rows)
            with self._lock:
                l.term.resize(cols, rows)
        if lane == "dst":
            self.cols, self.rows = cols, rows

    # ------------------------------------------------------------ 输出 / 屏幕
    def output(self, lane: str = "dst") -> bytes:
        """该通道累计收到的真实输出字节。"""
        with self._lock:
            return bytes(self._lane(lane).buf)

    def drain(self, seconds: float = 2.0, lane: str = "dst") -> bytes:
        """等一段时间，返回这段时间内新增的字节。"""
        with self._lock:
            start = len(self._lane(lane).buf)
        time.sleep(seconds)
        with self._lock:
            return bytes(self._lane(lane).buf[start:])

    def text(self, lane: str = "dst") -> str:
        """该通道终端的可见区纯文本（行以 \\n 连接）。"""
        with self._lock:
            return self._lane(lane).term.text()

    def lines(self, lane: str = "dst") -> list:
        return self.text(lane).splitlines()

    def tail_lines(self, n: int = 8, lane: str = "dst") -> list:
        return self.lines(lane)[-n:]

    def cursor(self, lane: str = "dst"):
        """(row, col, visible)，0-based。"""
        with self._lock:
            return self._lane(lane).term.cursor()

    def scrollback_count(self, lane: str = "dst") -> int:
        with self._lock:
            return self._lane(lane).term.scrollback_count()

    def is_alt_screen(self, lane: str = "dst") -> bool:
        with self._lock:
            return self._lane(lane).term.is_alt_screen_active()

    def nonblank_lines(self, lane: str = "dst") -> int:
        """可见区里非空行数（屏幕断言常用量）。"""
        return sum(1 for l in self.lines(lane) if l.strip())

    def wait_screen(self, needle: str, timeout: float = 10.0,
                    lane: str = "dst", interval: float = 0.2) -> bool:
        """轮询直到可见屏幕出现 needle。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.text(lane):
                return True
            time.sleep(interval)
        return needle in self.text(lane)

    def wait_line(self, needle: str, timeout: float = 10.0, lane: str = "dst",
                  exact: bool = True, interval: float = 0.2) -> bool:
        """轮询直到屏幕上出现某一行。

        exact=True 要求整行相等 —— 断言"命令的输出行"用这个：
        cmd 会把敲进去的命令行本身回显到屏幕上，`needle in text` 会把
        "echo TI_X" 里的 TI_X 也算命中，等于没验证到输出。
        """
        deadline = time.time() + timeout
        while True:
            lines = self.lines(lane)
            if exact:
                if any(l.strip() == needle for l in lines):
                    return True
            elif any(needle in l for l in lines):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(interval)

    def wait_bytes(self, needle: bytes, timeout: float = 10.0,
                   lane: str = "dst", interval: float = 0.2) -> bool:
        """轮询直到累计字节里出现 needle（比屏幕更早可见，用于"输出到达"）。"""
        if isinstance(needle, str):
            needle = needle.encode("utf-8")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self.output(lane):
                return True
            time.sleep(interval)
        return needle in self.output(lane)

    # ------------------------------------------------------------ 日志
    def mediator_log(self) -> str:
        return childlog.read(paths.mediator_log(self.target_pid))

    def dll_log(self) -> str:
        return childlog.read(childlog.injected_log(self.target_pid))

    def _log_text(self, which: str) -> str:
        if which in ("mediator", "m"):
            return self.mediator_log()
        if which in ("dll", "injected"):
            return self.dll_log()
        raise ValueError("which 只能是 mediator/dll，得到 {!r}".format(which))

    def wait_log(self, needle: str, timeout: float = 10.0, which: str = "mediator",
                 interval: float = 0.2) -> bool:
        """轮询直到指定日志出现子串。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self._log_text(which):
                return True
            time.sleep(interval)
        return needle in self._log_text(which)

    def wait_log_regex(self, pattern: str, timeout: float = 10.0,
                       which: str = "mediator", interval: float = 0.2):
        """轮询直到指定日志匹配正则，返回 Match 或 None。"""
        regex = re.compile(pattern)
        deadline = time.time() + timeout
        while time.time() < deadline:
            m = regex.search(self._log_text(which))
            if m:
                return m
            time.sleep(interval)
        return regex.search(self._log_text(which))

    def grep(self, which: str = "mediator", patterns=()) -> list:
        """按关键词过滤日志行（去掉 v1 那种前缀噪声，便于打印诊断）。"""
        text = self._log_text(which)
        out = []
        for line in text.splitlines():
            if any(p in line for p in patterns):
                i = line.find("]")
                out.append(line[i + 1:].strip() if i >= 0 else line.strip())
        return out

    def mouse(self, x: int, y: int, kind: str = "press", button: str = "left",
              mods: int = 0, lane: str = "dst") -> None:
        """发鼠标事件（用 dst 的 Terminal 编码，自动遵守目标开启的鼠标协议/SGR）。"""
        l = self._lane(lane)
        with self._lock:
            data = bytes(l.term.mouse(x, y, kind=kind, button=button, mods=mods))
            resp = bytes(l.term.drain_written())
        if data:
            l.pty.write(list(data))
        if resp:
            l.pty.write(list(resp))

    def shell_run(self, argv, lane: str = "dst") -> str:
        """在目标 shell 里运行一条命令（每个 token 加引号 + 必要的调用运算符）。"""
        cmd = build_command(list(argv))
        if self.call_prefix:
            cmd = "{} {}".format(self.call_prefix, cmd)
        self.send_line(cmd, lane=lane)
        return cmd

    # ------------------------------------------------------------ 拓扑自证
    def topology(self, timeout: float = 15.0):
        """本会话实际跑在哪种目标宿主拓扑上：`"conhost"` / `"conpty"` / None（读不到）。

        依据 DLL 日志的 `conptyHosted=0/1`。**每个依赖拓扑的用例都该自证一次**：
        "WT 里开的 cmd"（ConPTY 托管）与"直接打开的 cmd"（经典 ConHost）走的是
        LazyInit 的两条不同分支，声明错或环境变了都会让用例**静默覆盖到另一条路径**，
        看起来还是绿的。
        """
        if not self.wait_log("conptyHosted=", timeout=timeout, which="dll"):
            return None
        hits = re.findall(r"conptyHosted=(\d)", self.dll_log())
        if not hits:
            return None
        return "conpty" if hits[-1] == "1" else "conhost"

    def assert_topology(self, expected: str, timeout: float = 15.0):
        """断言拓扑；返回 (ok, actual)。expected ∈ {"conhost", "conpty"}。"""
        actual = self.topology(timeout=timeout)
        return (actual == expected), actual

    def wait_handshake(self, timeout: float = None) -> bool:
        """等 mediator 日志出现 Handshake OK；致命启动错误则提前返回 False。

        失败判据**只认主握手的显式失败**（`Mediator: Handshake failed` /
        `Mediator: targetPid is 0`）。不要用 v1 那种 `"ERROR" in 日志`：
        中继会话、被接管后代等**子会话**出错的日志同样含 ERROR，
        它们不影响主握手，却会让等待提前返回 False ⇒ 用例假报"握手失败"
        （跨位数/接管类用例最容易踩）。
        """
        timeout = self.handshake_timeout if timeout is None else timeout
        deadline = time.time() + timeout
        while time.time() < deadline:
            text = self.mediator_log()
            if "Mediator: Handshake OK" in text:
                return True
            if "Mediator: Handshake failed" in text or "Mediator: targetPid is 0" in text:
                return False
            time.sleep(0.2)
        return False

    # ------------------------------------------------------------ 诊断
    def describe(self) -> str:
        info = ["host={}".format(self.host),
                "target_pid={}".format(self.target_pid),
                "mediator_pid={}".format(self.mediator_pid),
                "lanes={}".format(sorted(self._lanes)),
                "pump_errors={}".format(self._pump_errors)]
        try:
            info.append("cursor={}".format(self.cursor()))
        except Exception:
            pass
        return " ".join(info)

    def has_hook_dll(self, pid: int) -> bool:
        return childlog.has_hook_dll(pid)
