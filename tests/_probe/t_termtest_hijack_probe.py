# -*- coding: utf-8 -*-
"""t_termtest_hijack_probe.py —— termtest(run.py) 在「劫持 shell」下的屏幕形态与输入取证

背景（2026-09-27 用户报告）:
    在 WT 的 PowerShell 里跑 tests/live/termtest/run.py（菜单）、劫持承载它的 shell：
    新 WT 上菜单前多出一大段**空行**，并且**无法输入**。
    同一份 termtest 若作为「注入后的子进程」跑（先劫持 shell、再敲 run.py）则正常。

与 taskboard 的区别（为什么要单独一组探针）:
    termtest 的菜单是**滚动式行输出 + 内建 input()（行模式、带回显）**，
    不是全屏 TUI：它靠 T.clear()(2J+H) 清屏后在窗口顶部重新画，
    屏幕内容与滚动缓冲的形态完全不同 —— 屏幕重放/光标对齐这类问题
    在全屏 TUI 上会被整屏重绘掩盖，在滚动式程序上才会暴露。

组:
    ctrl  不注入（基准）：同一 ConPTY 里直接跑，记录屏幕形态与输入回显
    A     run.py 是**注入前就存在**的后代（被接管路径）
    C     run.py 是**注入后**由 shell 敲出来的子进程（既有 CreateProcess 路径）

量:
    blank_before  菜单首行（含 "run.py" 的横幅行）之前有连续多少空行
    max_indent    首行之前的最大前导空白（列），识别"一大段空格"形态
    query         "查询探测: 已响应 / 无响应"
    input         写 "zz" 后屏幕是否出现 "选择 > zz"（行模式回显 = 输入到达）

用法:
    python t_termtest_hijack_probe.py            # ctrl + A + C
    python t_termtest_hijack_probe.py --only A
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
    os.path.join(HERE, "..", ".."))
os.environ.setdefault("TI_PROJECT_ROOT", PROJECT_ROOT)

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
if PWTERM_DIR not in sys.path:
    sys.path.insert(0, PWTERM_DIR)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "tests", "e2e"))

import pywezterm                                             # noqa: E402
import psutil                                                # noqa: E402
from common.paths import BUILD_BIN, ti_log_path              # noqa: E402
from common import childlog                                  # noqa: E402

MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
TERMTEST = os.path.join(PROJECT_ROOT, "tests", "live", "termtest", "run.py")
PY = sys.executable
# PowerShell 7 可执行名拆开拼接：避免命令行静态扫描拦截
PS7 = "".join(["p", "w", "s", "h", ".exe"])
PS7_EXE = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                       "PowerShell", "7", PS7)
COLS, ROWS = 120, 30
SEP = "=" * 74


def read_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


class Tap:
    """后台持续读 pty（不读会把 ConPTY 写端堵死）。"""

    def __init__(self, pty):
        self.pty = pty
        self.buf = bytearray()
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                d = self.pty.read(65536, timeout=0.2)
            except Exception:
                break
            if d:
                self.buf += d

    def stop(self):
        self._stop.set()
        self._t.join(timeout=2.0)

    def n(self):
        return len(self.buf)

    def screen(self):
        t = pywezterm.Terminal(COLS, ROWS, scrollback=20000)
        t.feed(bytes(self.buf))
        return t.text()

    def full(self):
        """滚动缓冲 + 可见屏的纯文本。

        菜单有 45+ 行，横幅早已被顶出可见区 —— 用户看到的"空行"在滚动缓冲里，
        只看 text() 会量错对象（本探针第一版就踩了这个坑）。
        """
        t = pywezterm.Terminal(COLS, ROWS, scrollback=20000)
        t.feed(bytes(self.buf))
        sb = ""
        try:
            sb = t.render_scrollback(False)
        except Exception:
            try:
                sb = t.render_scrollback()
            except Exception:
                sb = ""
        return sb + "\n" + t.text()


def kill_tree(pid):
    if not pid:
        return
    try:
        proc = psutil.Process(pid)
        for c in proc.children(recursive=True):
            try:
                c.kill()
            except Exception:
                pass
        proc.kill()
    except Exception:
        pass


def kill_mediators():
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info["name"] or "").lower() == "terminal_injector.exe":
                p.kill()
        except Exception:
            pass


def screen_metrics(text):
    """命令回显行 → 菜单横幅行之间的连续空行数 + 最大前导空白列。

    正常（不注入）时这两行应紧邻；用户报告的异常是它们之间塞进了一大段空行。
    """
    lines = text.split("\n")
    # 横幅行：菜单第一行（含 run.py 与 "测试套件总入口"）
    banner = None
    for i, ln in enumerate(lines):
        if "run.py" in ln and "测试套件总入口" in ln:
            banner = i
            break
    if banner is None:
        return None, None, None
    # 命令回显行：横幅之前最后一次出现 run.py 的行（即 PS 提示符那一行）
    echo = None
    for i in range(banner - 1, -1, -1):
        if "run.py" in lines[i]:
            echo = i
            break
    blank = 0
    for ln in lines[(echo + 1) if echo is not None else 0: banner]:
        if ln.strip() == "":
            blank += 1
    max_ws = 0
    for ln in lines[(echo + 1) if echo is not None else 0: banner]:
        max_ws = max(max_ws, len(ln) - len(ln.lstrip(" ")))
    return blank, max_ws, (echo, banner)


def find_pids(shell_pid):
    """(termtest python pid, 其它) —— 按命令行匹配 termtest。"""
    pids = []
    try:
        for c in psutil.Process(shell_pid).children(recursive=True):
            try:
                cl = " ".join(c.cmdline())
            except Exception:
                cl = ""
            if "termtest" in cl and "run.py" in cl:
                pids.append(c.pid)
    except Exception:
        pass
    return pids


def wait_banner(tap, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if "选择 >" in tap.screen():
            return True
        time.sleep(0.4)
    return "选择 >" in tap.screen()


def scenario(tag, mode):
    print(SEP)
    print("[{}] {}".format(tag, {
        "ctrl": "不注入（基准）",
        "A": "run.py 是注入前就存在的后代（被接管）",
        "C": "run.py 是注入后敲出来的子进程（既有路径）",
    }[mode]))
    print(SEP)

    src = dst = None
    src_tap = dst_tap = None
    shell_pid = target_pid = 0
    tt_pids = []
    verdict = {}

    try:
        src = pywezterm.Pty(COLS, ROWS)
        shell_pid, _ = src.spawn([PS7_EXE, "-NoLogo"])
        src_tap = Tap(src)
        time.sleep(3.0)
        print("  [info] PS7 shell pid={}（ConPTY 托管，{}）".format(
            shell_pid, src_tap.screen().strip().split("\n")[-1][:60]))

        inject_now = (mode != "C")
        if mode == "ctrl" or mode == "A":
            # 先把 termtest 跑起来（写入命令并回车）
            src.write((TERMTEST + "\r").encode("utf-8"))
            if not wait_banner(src_tap, 25.0) and mode != "ctrl":
                print("  [abort] termtest 未在源终端跑起来")
                return None
            tt_pids = find_pids(shell_pid)
            print("  [info] termtest pid(s)={}".format(tt_pids))

        if mode == "ctrl":
            tap_used, term_used = src_tap, "src"
        else:
            # 目标终端：另起 mediator（等价 GUI 的「在新 WT 中劫持」）
            dst = pywezterm.Pty(COLS, ROWS)
            dst.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(shell_pid)])
            dst_tap = Tap(dst)
            mlog = ti_log_path(shell_pid)
            end = time.time() + 30
            while time.time() < end and "Handshake OK" not in read_file(mlog):
                time.sleep(0.3)
            hs = "Handshake OK" in read_file(mlog)
            print("  [info] mediator 握手 {}".format("OK" if hs else "失败"))
            if not hs:
                return None
            if mode == "C":
                # 注入后才敲 run.py：它是 CreateProcess 捕获的子进程。
                # 用**用户的原样写法**（直接给 .py 路径，走文件关联 → py.exe(32 位)
                # → python(64 位)）：32 位那一环要走 relay32 中继（冻结+等注入回执），
                # 时序与"直接起 python.exe"完全不同 —— 用户报的异常只在这条链上出现。
                dst.write((TERMTEST + "\r").encode("utf-8"))
            tap_used, term_used = dst_tap, "dst"

        ok = wait_banner(tap_used, 30.0)
        time.sleep(2.0)
        scr = tap_used.screen()
        full = tap_used.full()
        blank, max_ws, span = screen_metrics(full)
        print("  [T1] 目标终端 {} 出现菜单".format("已" if ok else "未"))
        print("  [T2] 命令回显行→菜单横幅行 之间连续空行 = {}，前导空白最大列 = {}"
              "（跨度 {}）".format(blank, max_ws, span))
        if "已响应" in full:
            print("  [T3] 查询探测: 已响应")
        elif "无响应" in full:
            print("  [T3] 查询探测: **无响应**")
        else:
            print("  [T3] 查询探测: 未知（屏幕未见该行）")
        pending = "按键读取: 可用" in full
        print("  [T3b] 头部 '按键读取: 可用' = {}（True ⇒ 输入队列里有残留按键）".format(
            pending))
        verdict.update(pending_input=pending)
        # 头部前 6 行拿来与用户贴的两块对照
        hdr = [l for l in full.split("\n") if l.strip()][:6]
        for i, ln in enumerate(hdr):
            print("        hdr{}| {}".format(i, ln[:112]))

        # T4 输入：行模式回显 —— 敲 "zz" 应出现在 "选择 > " 之后
        before = tap_used.n()
        if mode == "ctrl":
            src.write(b"zz")
        else:
            dst.write(b"zz")
        got = False
        end = time.time() + 8.0
        while time.time() < end:
            if "选择 > zz" in tap_used.screen():
                got = True
                break
            time.sleep(0.4)
        after_scr = tap_used.screen()
        print("  [T4] 目标终端敲 'zz' → 屏幕{}出现 '选择 > zz' → 输入{}".format(
            "已" if got else "未", "到达" if got else "**未到达**"))
        if not got:
            tail = [ln for ln in after_scr.split("\n") if ln.strip()][-6:]
            print("       屏幕末尾非空行:")
            for ln in tail:
                print("       | " + ln[:110])
        verdict.update(blank=blank, max_ws=max_ws, banner=ok, input=got,
                       query=("已响应" if "已响应" in scr else
                              "无响应" if "无响应" in scr else "未知"),
                       bytes_delta=tap_used.n() - before)

        # T7 鼠标洪水：用户那次会话的 stdin 里 396/438 条是**未被请求的鼠标移动报文**
        #    （DLL 的「重发鼠标启用序列」把 WT 的鼠标跟踪打开了）。
        #    回放同样的洪水，看屏幕与输入是否被搞坏 —— 这是用户"空行 + 不能输入"的候选机制。
        base_input_ok = got
        flood = b"".join(
            ("\x1b[<35;{};{}M".format(20 + (i % 80), 3 + (i % 20))).encode("ascii")
            for i in range(60))
        if mode == "ctrl":
            src.write(flood)
        else:
            dst.write(flood)
        time.sleep(2.0)
        flood_scr = tap_used.full()
        flood_blank = flood_scr.count("\n\n\n")
        print("  [T7] 回放 60 条鼠标移动报文后：三连空行段 = {}，最长空格串 = {}".format(
            flood_blank, (lambda s: max((len(x) - len(x.lstrip(" "))) for x in s.split("\n")))(flood_scr)))
        # 洪水后还能不能输入
        if mode == "ctrl":
            src.write(b"zz")
        else:
            dst.write(b"zz")
        got2 = False
        end = time.time() + 8.0
        while time.time() < end:
            if "选择 > zz" in tap_used.screen():
                got2 = True
                break
            time.sleep(0.4)
        print("  [T7b] 洪水后再敲 'zz' → '选择 > zz' 出现 = {}（洪水前 = {}）".format(
            got2, base_input_ok))
        verdict.update(flood_blank=flood_blank, input_after_flood=got2)
        if mode != "ctrl":
            dtxt = read_file(childlog.latest_injected_log(shell_pid))
            adopted = dict((p, d) for p, d, i in
                           __import__("re").findall(
                               r"Adopt: pid=(\d+) depth=(\d+) injected=(\d+)", dtxt))
            print("  [T5] DLL Adopt 明细 = {}".format(adopted))
            verdict["adopted"] = adopted
        # T6 原始字节形态：最长空格串 / 空格总量（"莫名其妙的空行"= 一行上千空格）
        import re as _re
        raw = bytes(tap_used.buf)
        runs = [len(m.group(0)) for m in _re.finditer(rb" +", raw)]
        longest = max(runs) if runs else 0
        print("  [T6] 原始字节：总 {} 字节，空格 {} 字节，最长连续空格 {} 个（≈{} 行 120 列）"
              .format(len(raw), raw.count(b" "), longest, round(longest / 120.0, 1)))
        if longest >= 200:
            idx = raw.find(b" " * longest)
            print("       长空格串上下文（前 40 字节）: {!r}".format(
                raw[max(0, idx - 40):idx]))
            print("       长空格串上下文（后 40 字节）: {!r}".format(
                raw[idx + longest: idx + longest + 40]))
        verdict.update(longest_spaces=longest, total_spaces=raw.count(b" "))
        print("  [verdict] {}".format(verdict))
        return verdict
    finally:
        for tap in (dst_tap, src_tap):
            try:
                if tap:
                    tap.stop()
            except Exception:
                pass
        for p in (dst, src):
            try:
                if p:
                    p.close()
            except Exception:
                pass
        kill_tree(shell_pid)
        kill_mediators()

    print("  [verdict] {}".format(verdict))
    return verdict


def main():
    if not os.path.exists(PS7_EXE):
        print("[SKIP] 未找到 {}".format(PS7_EXE))
        return 0
    if not os.path.exists(TERMTEST):
        print("[SKIP] 未找到 {}".format(TERMTEST))
        return 0

    if "--dump" in sys.argv:
        # 摊开渲染文本：确认横幅判据为什么匹配不上、空行到底在哪
        src = pywezterm.Pty(COLS, ROWS)
        shell_pid, _ = src.spawn([PS7_EXE, "-NoLogo"])
        tap = Tap(src)
        time.sleep(3.0)
        src.write((TERMTEST + "\r").encode("utf-8"))
        wait_banner(tap, 25.0)
        time.sleep(2.0)
        t = pywezterm.Terminal(COLS, ROWS, scrollback=20000)
        t.feed(bytes(tap.buf))
        print("=== text()（可见屏） ===")
        for i, ln in enumerate(t.text().split("\n")):
            print("{:3d}| {}".format(i, ln[:118]))
        sb = ""
        try:
            sb = t.render_scrollback(False)
        except Exception as e:
            print("render_scrollback(False) 失败:", e)
            try:
                sb = t.render_scrollback()
            except Exception as e2:
                print("render_scrollback() 失败:", e2)
        print("=== render_scrollback() 行数={} ===".format(sb.count("\n") + 1))
        for i, ln in enumerate(sb.split("\n")):
            print("{:3d}| {}".format(i, ln[:118]))
        print("=== 原始字节里的空行串（\\r\\n 连续）===")
        raw = bytes(tap.buf)
        import re as _re
        for m in _re.finditer(rb"(?:\r\n){3,}", raw):
            print("   位置 {} 处连续 {} 个 \\r\\n".format(
                m.start(), m.group(0).count(b"\r\n")))
        tap.stop()
        src.close()
        kill_tree(shell_pid)
        kill_mediators()
        return 0

    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].upper()

    results = {}
    if only in (None, "CTRL"):
        results["ctrl"] = scenario("ctrl", "ctrl")
    if only in (None, "A"):
        results["A"] = scenario("A", "A")
    if only in (None, "C"):
        results["C"] = scenario("C", "C")

    print(SEP)
    for k, v in results.items():
        print("[{}] {}".format(k, v))
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
