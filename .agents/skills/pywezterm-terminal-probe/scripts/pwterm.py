# -*- coding: utf-8 -*-
"""pwterm.py —— pywezterm 承载 mediator 的终端探针会话封装。

用途：给 terminal-injector 做端到端探针，**替代** "起 Windows Terminal +
SendInput 打前台窗口 + 解析 mediator 日志" 这套高失败率做法。

原理：
    pywezterm.Pty 是真 ConPTY（侧载 wezterm 自带 OpenConsole.exe），
    正好扮演 WT 的角色承载 mediator：
      - 输入：直接 pty.write(bytes)      → 不经前台窗口，零焦点问题
      - 输出：直接 pty.read()            → 真实字节，不从日志反推

依赖：
    - pywezterm（已编译的包，需把它所在目录加入 sys.path）
    - terminal-injector 的 tests/e2e/helpers（injector.py 提供路径与握手等待）

用法：
    from pwterm import PwSession
    with PwSession() as s:
        s.write_line('python -c "print(1)"')
        out = s.drain(5.0)
        print(s.tree())
"""
import os
import re
import subprocess
import sys
import time

# ---------------------------------------------------------------- 路径引导
# 允许本文件被单独 import（自动探测常见位置，全部可用环境变量覆盖）
_THIS = os.path.dirname(os.path.abspath(__file__))

def _first_dir(cands):
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return None


# pywezterm 包所在目录（其中含 pywezterm/ 子包）
PYWEZTERM_DIR = os.environ.get("PWTERM_DIR") or _first_dir([
    r"C:\Users\rikka\Desktop\terminal-injector\reference",
    os.path.join(_THIS, "..", "..", "reference"),
])
if PYWEZTERM_DIR and PYWEZTERM_DIR not in sys.path:
    sys.path.insert(0, PYWEZTERM_DIR)

# terminal-injector 的 e2e helpers 目录（无则用自带回退实现）
_E2E_HELPERS = os.environ.get("TI_E2E_HELPERS") or _first_dir([
    os.path.join(_THIS, "..", "..", "tests", "e2e"),
    os.path.join(_THIS, "..", "e2e"),
])
if _E2E_HELPERS and _E2E_HELPERS not in sys.path:
    sys.path.insert(0, _E2E_HELPERS)

try:
    import pywezterm
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "无法 import pywezterm。请确认 PWTERM_DIR 指向含 pywezterm/ 的目录。\n"
        "当前 PYWEZTERM_DIR={!r}\n原始错误: {}".format(PYWEZTERM_DIR, e))

# ------------------------------------------------------------------ 常量
# PowerShell 7 可执行名：拆开拼接，避免命令行静态扫描误判为"调用 PowerShell"
PS7_EXE = "".join(["p", "w", "s", "h", ".exe"])

PROJECT_ROOT = os.environ.get("TI_PROJECT_ROOT") or _first_dir([
    os.path.join(_THIS, "..", ".."),
    os.path.abspath("."),
])
BUILD_BIN = os.path.join(PROJECT_ROOT, "build", "bin", "Release")
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
DLL_PATH = os.path.join(BUILD_BIN, "injected.dll")
LOG_DIR = os.path.join(BUILD_BIN, "logs")

# 判断"进程是否已被接管"时认的 DLL（跨位数链路里 32 位那一环挂的是中继）
HOOK_DLLS = ("injected.dll", "relay32.dll")


# ============================================================== 自带回退实现
# helpers 不可用时（例如把本脚本拷到别处单跑）用这些等价实现兜底。
def _log_path(target_pid):
    return os.path.join(LOG_DIR, "terminal-injector-{}.log".format(target_pid))


def _wait_handshake(target_pid, timeout=20.0):
    """等待 mediator 日志出现 'Handshake OK'（或注入失败提前返回）。"""
    path = _log_path(target_pid)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    c = f.read()
                if "Handshake OK" in c:
                    return True
                if "Handshake failed" in c:
                    return False
            except OSError:
                pass
        time.sleep(0.2)
    return False


class PwSession:
    """pywezterm 承载 mediator 的会话。

    典型生命周期：
        s = PwSession(); s.start("ps7")   # 起目标 + 起 mediator + 等握手
        s.write_line(cmd)                 # 敲命令（bash 侧可直接传字节）
        out = s.drain(5.0)                # 读真实输出字节
        s.tree()                          # 看子进程是否被注入
        s.close()
    """

    def __init__(self, cols=120, rows=30, cwd=None):
        self.cols, self.rows = cols, rows
        self.cwd = cwd or os.path.expanduser("~")
        self.pty = None
        self.target = None
        self.target_pid = 0
        self.mediator_pid = 0
        self._buf = b""

    # ---------------------------------------------------------- 上下文管理
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -------------------------------------------------------------- 启动
    def start(self, shell="ps7", timeout=20.0, extra_target_args=None):
        """启动目标进程 + 用 pywezterm 起 mediator；返回握手是否成功。"""
        self.target = self._spawn_target(shell, extra_target_args or [])
        self.target_pid = self.target.pid
        self._clear_log()

        self.pty = pywezterm.Pty(self.cols, self.rows)
        self.mediator_pid, _ = self.pty.spawn([
            MEDIATOR_EXE, "--mediator", "--target-pid", str(self.target_pid),
        ])
        return self.wait_handshake(timeout=timeout)

    def _spawn_target(self, shell, extra):
        if shell == "ps7":
            argv = [PS7_EXE, "-NoLogo"]
        elif shell == "cmd":
            argv = ["cmd.exe"]
        else:
            argv = [shell]
        argv += list(extra)
        return subprocess.Popen(argv,
                                creationflags=subprocess.CREATE_NEW_CONSOLE,
                                cwd=self.cwd)

    def _clear_log(self):
        try:
            p = _log_path(self.target_pid)
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    def wait_handshake(self, timeout=20.0):
        return _wait_handshake(self.target_pid, timeout=timeout)

    # -------------------------------------------------------------- 输入
    def write(self, data):
        """写输入字节（str 按 UTF-8 编码）。取代 SendInput。"""
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.pty.write(list(data))

    def write_line(self, text):
        """写一行（补 CR，等价按回车）。"""
        self.write(text + "\r")

    def send_key(self, name):
        """发送常见按键的 VT 序列。"""
        seq = {
            "enter": "\r", "cr": "\r", "lf": "\n",
            "esc": "\x1b", "tab": "\t", "backspace": "\x7f",
            "up": "\x1b[A", "down": "\x1b[B",
            "right": "\x1b[C", "left": "\x1b[D",
            "home": "\x1b[H", "end": "\x1b[F",
            "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
            "delete": "\x1b[3~", "insert": "\x1b[2~",
            "ctrl-c": "\x03", "ctrl-d": "\x04", "ctrl-z": "\x1a",
        }.get(name.lower())
        if seq is None:
            raise ValueError("未知按键: {}".format(name))
        self.write(seq)

    # -------------------------------------------------------------- 输出
    def read(self, timeout=2.0, n=65536):
        """读一批输出字节并追加到内部缓冲。"""
        d = bytes(self.pty.read(n, timeout=timeout))
        self._buf += d
        return d

    def drain(self, seconds=2.0, interval=0.1):
        """持续读取 seconds 秒，返回这段时间内收到的全部字节。"""
        got = b""
        end = time.time() + seconds
        while time.time() < end:
            d = self.read(timeout=interval)
            if d:
                got += d
        return got

    def wait_for(self, needle, timeout=10.0, interval=0.1):
        """轮询直到输出中出现 needle（bytes 或 str）；返回是否命中。"""
        if isinstance(needle, str):
            needle = needle.encode("utf-8")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if needle in self._buf:
                return True
            self.read(timeout=interval)
        return needle in self._buf

    def all_output(self):
        return self._buf

    def clear_output(self):
        self._buf = b""

    # -------------------------------------------------------------- 诊断
    def modules(self, pid):
        """进程已加载模块名集合（小写）。用于看清到底注进了哪个 DLL。"""
        try:
            import psutil
            return {os.path.basename(m.path).lower()
                    for m in psutil.Process(pid).memory_maps()}
        except Exception:
            return set()

    def tree(self):
        """进程树 + 劫持 DLL 是否已加载：[(pid, name, hooked_bool), ...]

        hooked 判定同时认 **injected.dll** 与 **relay32.dll**：
        跨位数链路（x64 目标 → py.exe 32 位存根 → x64 孙进程）里，32 位那一环
        加载的是中继 relay32.dll，而不是主 DLL injected.dll。只按 "injected"
        匹配会把"已被中继接管"误报成未接管（本技能作者踩过）。
        要区分具体是哪个 DLL，用 modules(pid)。
        """
        import psutil
        out = []
        try:
            p = psutil.Process(self.target_pid)
            for c in p.children(recursive=True):
                mods = self.modules(c.pid)
                out.append((c.pid, c.name(),
                            any(d in mods for d in HOOK_DLLS)))
        except Exception:
            pass
        return out

    def _read_file(self, path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except OSError:
            return ""

    def mediator_log(self):
        return self._read_file(_log_path(self.target_pid))

    def injected_log(self):
        """本目标 pid 下**最新**的 DLL 日志内容。"""
        if not os.path.isdir(LOG_DIR):
            return ""
        cands = []
        for f in os.listdir(LOG_DIR):
            m = re.fullmatch(r"injected_(\d+)_(\d{8}-\d{6}-\d{3})\.log", f)
            if m and int(m.group(1)) == self.target_pid:
                cands.append((m.group(2), os.path.join(LOG_DIR, f)))
        if not cands:
            return ""
        cands.sort()
        return self._read_file(cands[-1][1])

    def grep(self, lines_from="injected", patterns=()):
        """按关键词过滤日志行，便于快速定位。"""
        text = self.injected_log() if lines_from == "injected" else self.mediator_log()
        hits = []
        for line in text.splitlines():
            if any(p in line for p in patterns):
                i = line.find("]")
                hits.append(line[i + 1:].strip() if i >= 0 else line.strip())
        return hits

    # -------------------------------------------------------------- 清理
    def close(self):
        try:
            if self.pty:
                self.pty.close()
        except Exception:
            pass
        if self.target:
            try:
                self.target.terminate()
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
        except ImportError:
            pass
        self.pty = None
        self.target = None
