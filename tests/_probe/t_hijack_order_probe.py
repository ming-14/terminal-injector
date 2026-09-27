# -*- coding: utf-8 -*-
"""t_hijack_order_probe.py —— 「先在 WT 里跑起 TUI，再劫持」的输出/输入通路取证

背景（2026-09-27 用户报告）
    开 WT → 在 WT 里 `python tests/live/textual/taskboard.py` → 劫持到新 WT 后：
    新 WT 上 TUI 画面定格、按键无效；此时卸载回原 WT，TUI 立刻恢复正常。
    用户强调 2026-08-30 走同一流程是正常的。

本探针要回答的唯一问题：**被劫持的目标是谁**，决定了断点在哪里 ——
    A 组  target = 承载 TUI 的 shell（cmd /k python taskboard.py）
          TUI 是「注入前就已存在的后代」，CreateProcess Hook 覆盖不到它
    B 组  target = TUI 自身（python.exe，shell 的子进程）

两组用同一套源会话（真 ConPTY 承载，等价于 WT 的 ConPTY），只换注入目标。

判据（逐项打印，不做推测）
    T1 源终端：注入前能否看到 TUI 画起来（用例前提）
    T2 目标终端：能否收到重放帧（注入链路本身是否工作）
    T3 目标终端：注入后每秒是否持续收到新字节（**画面是否刷新**）
    T4 源终端：注入后是否仍在收到字节（输出是否还写旧 ConHost —— T3 失败时的铁证）
    T5 输入：向目标终端发 'q'，TUI 是否退出（按键是否真的到达 TUI）
    T6 日志：DLL 侧 Adopt/重放记录 + mediator 侧子会话记录

用法：
    python t_hijack_order_probe.py            # 跑 A、B 两组
    python t_hijack_order_probe.py --only A
"""
import os
import re
import subprocess
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

import pywezterm                                            # noqa: E402
import psutil                                               # noqa: E402
from common.paths import BUILD_BIN, RESULTS_DIR, ti_log_path     # noqa: E402
from common import childlog                                 # noqa: E402

MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
COMSPEC = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "cmd.exe")
TASKBOARD = os.path.join(PROJECT_ROOT, "tests", "live", "textual", "taskboard.py")
PY = sys.executable
COLS, ROWS = 120, 30

SEP = "=" * 74

# GUI 型后代脚本：有可见顶层窗口、完全不碰控制台 —— 就是 2026-09-27 那次
# 严重回归里被误接管的那个角色（从同一个 shell 里起的 tkinter 管理界面）。
# 窗口放到屏幕外，避免测试时打扰用户桌面；IsWindowVisible 仍为真。
GUI_LIKE = r'''
import tkinter as tk
root = tk.Tk()
root.title("ti-probe-gui-like")
root.geometry("120x80+4000+4000")
root.after(120000, root.destroy)
root.mainloop()
'''

# 同控制台里"先起 GUI、再起屏幕占用者"的启动脚本。
# start /b：不新建窗口、继承同一个控制台，父进程仍是 cmd。
SH_CMD = '''@echo off
start "" /b "{py}" -u "{gui}"
"{py}" -u "{tui}"
'''


# ----------------------------------------------------------------- 小工具
def read_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


class Tap:
    """后台持续读一个 pty 并累积字节。

    必须持续读：源会话是 ConPTY，输出管道写满会把 TUI 冻住，
    那时"画面不刷新"就成了探针自己造出来的假象。
    """

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
        """用 pywezterm.Terminal（wezterm-term 本体，与 WT 同源）还原可见屏幕。"""
        t = pywezterm.Terminal(COLS, ROWS)
        t.feed(bytes(self.buf))
        return t.text()

    def tail(self, n=160):
        return bytes(self.buf[-n:])


def wait_screen_in(tap, needle, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if needle in tap.screen():
            return True
        time.sleep(0.3)
    return False


def wait_log_contains(path, needle, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if needle in read_file(path):
            return True
        time.sleep(0.3)
    return False


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


# ----------------------------------------------------------------- 单组实验
def scenario(tag, target_mode, with_gui=False):
    print(SEP)
    print("[{}] target = {}{}".format(
        tag, "承载 TUI 的 shell（cmd /k python taskboard.py）"
        if target_mode == "shell" else "TUI 自身（python.exe）",
        "，同控制台另有 GUI 型后代" if with_gui else ""))
    print(SEP)

    src = dst = None
    src_tap = dst_tap = None
    shell_pid = py_pid = target_pid = gui_pid = 0
    verdict = {}

    try:
        # ---- 源会话：等价于「开 WT → 在里面跑 taskboard.py」 --------------
        src = pywezterm.Pty(COLS, ROWS)
        if with_gui:
            os.makedirs(RESULTS_DIR, exist_ok=True)
            gui_script = os.path.join(RESULTS_DIR, "probe_gui_like.py")
            with open(gui_script, "w", encoding="utf-8") as f:
                f.write(GUI_LIKE)
            cmd_file = os.path.join(RESULTS_DIR, "probe_with_gui.cmd")
            with open(cmd_file, "w", encoding="ascii") as f:
                f.write(SH_CMD.format(py=PY, gui=gui_script, tui=TASKBOARD))
            shell_pid, _ = src.spawn([COMSPEC, "/k", cmd_file])
        else:
            shell_pid, _ = src.spawn([COMSPEC, "/k", PY, "-u", TASKBOARD])
        src_tap = Tap(src)
        time.sleep(1.5)
        for c in psutil.Process(shell_pid).children(recursive=True):
            try:
                cl = " ".join(c.cmdline())
            except Exception:
                cl = ""
            if "gui_like" in cl:
                gui_pid = c.pid
            elif c.name().lower().startswith("python"):
                py_pid = c.pid
        print("  [info] shell pid={}  TUI(python) pid={}  GUI-like pid={}".format(
            shell_pid, py_pid, gui_pid))

        ok = wait_screen_in(src_tap, "TaskBoard", 30.0)
        print("  [T1] 源终端 {} 看到 TUI 标题 'TaskBoard'（{} 字节）".format(
            "已" if ok else "未", src_tap.n()))
        if not ok:
            print("  [abort] 用例前提不成立：TUI 没在源会话里画起来")
            print("  src 尾字节: {!r}".format(src_tap.tail(300)))
            return None

        target_pid = shell_pid if target_mode == "shell" else py_pid
        if not target_pid:
            print("  [abort] 没拿到目标 pid")
            return None

        # ---- 目标终端：另一个 ConPTY 承载 mediator（等价于 GUI 的"新 WT tab"）----
        # 注入由 mediator 自己拉起注入器完成（GUI launch_in_wt 就是这个动作，
        # 管道名由 mediator 生成；事先手工 --inject 会把 DLL 引到另一条管道上）。
        dst = pywezterm.Pty(COLS, ROWS)
        dst.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])
        dst_tap = Tap(dst)
        print("  [info] 已在新终端拉起 mediator（注入 target pid={}）".format(target_pid))

        mlog = ti_log_path(target_pid)
        hs = wait_log_contains(mlog, "Handshake OK", 30.0)
        src_at_inject = src_tap.n()
        print("  [info] mediator 握手 {}".format("OK" if hs else "失败"))
        if not hs:
            print("  [abort] 握手失败，见 {}".format(mlog))
            return None

        # ---- T2：重放帧 --------------------------------------------------
        got_title = wait_screen_in(dst_tap, "TaskBoard", 12.0)
        print("  [T2] 目标终端 {} 收到重放帧（画面含 'TaskBoard'，{} 字节）".format(
            "已" if got_title else "未", dst_tap.n()))
        verdict["T2_replay"] = got_title

        # ---- T3/T4：注入后是否持续刷新 ----------------------------------
        time.sleep(3.0)          # 先让重放/首帧落定，再量"稳态是否还在动"
        base_dst, base_src = dst_tap.n(), src_tap.n()
        dst_buckets, src_buckets = [], []
        prev_d, prev_s = base_dst, base_src
        for _ in range(5):
            time.sleep(1.0)
            cur_d, cur_s = dst_tap.n(), src_tap.n()
            dst_buckets.append(cur_d - prev_d)
            src_buckets.append(cur_s - prev_s)
            prev_d, prev_s = cur_d, cur_s

        dst_alive = sum(1 for b in dst_buckets if b > 0)
        src_alive = sum(1 for b in src_buckets if b > 0)
        print("  [T3] 目标终端 5 秒内每秒新增字节 = {} → 画面{}".format(
            dst_buckets, "在刷新" if dst_alive >= 4 else "已定格"))
        print("  [T4] 源终端   5 秒内每秒新增字节 = {} → 输出{}".format(
            src_buckets, "仍在写旧 ConHost" if src_alive >= 4 else "已不再写旧 ConHost"))
        verdict["T3_dst_flow"] = dst_buckets
        verdict["T4_src_flow"] = src_buckets

        # ---- T5：输入是否到达 TUI ----------------------------------------
        # 判据标定（python t_hijack_order_probe.py --calib，未注入的对照组）：
        # taskboard 启动后焦点在 #filter 输入框里，**所有可打印字符都被它吃掉** ——
        # 'q' 不会退出、方向键也不生效；而敲进去的字符串会**显示在屏幕上**
        # （实测屏幕出现 `▊  qqqa?  ▎`）。故判据 = 屏幕出现本次独有的标记串：
        # 既不受控件焦点影响，也不会被定时器重绘伪造。
        token = "ti{}".format(int(time.time()) % 100000)
        before_in = dst_tap.screen()
        dst.write(token.encode("utf-8"))
        got_in = False
        end = time.time() + 8.0
        while time.time() < end:
            if token in dst_tap.screen():
                got_in = True
                break
            time.sleep(0.25)
        print("  [T5] 目标终端发 {!r} → 屏幕{}出现该串 → 按键{}".format(
            token, "已" if got_in else "未", "到达 TUI" if got_in else "未到达"))
        if not got_in:
            print("       目标端屏幕（前 12 行）：")
            for ln in before_in.split("\n")[:12]:
                print("       | " + ln)
        verdict["T5_input"] = got_in

        # ---- T6：日志判据 ------------------------------------------------
        dll_text = read_file(childlog.latest_injected_log(target_pid))
        adopt = re.search(r"Adopt: console has (\d+) process\(es\)", dll_text)
        print("  [T6] target DLL 日志：{}".format(
            "同控制台 {} 个进程".format(adopt.group(1)) if adopt
            else "无 Adopt 记录（当前代码没有这条路径）"))
        adopted = re.findall(r"Adopt: pid=(\d+) depth=(-?\d+) injected=(\d+)", dll_text)
        if adopted:
            print("       接管明细: {}".format(adopted))
        for line in dll_text.splitlines():
            if "Adopt: skip" in line:
                print("       跳过: {}".format(line.split("] ", 1)[-1].strip()))
        replay = re.search(r"screen content replayed to WT, (\d+) bytes", dll_text)
        print("       screen replay: {}".format(replay.group(1) + " bytes" if replay
                                                else "无记录"))
        mtext = read_file(mlog)
        print("       mediator: ChildProcessNotify={} 子会话数={}".format(
            "OnChildProcessNotify" in mtext, mtext.count("ChildSession started")))
        verdict["T6_adopted"] = adopted

        # ---- T7：GUI 型后代不得被接管（2026-09-27 严重回归的回归判据）------
        if with_gui:
            adopted_pids = {a[0] for a in adopted}
            gui_hooked = False
            if gui_pid:
                try:
                    mods = {m.path.lower().split("\\")[-1]
                            for m in psutil.Process(gui_pid).memory_maps()}
                    gui_hooked = "injected.dll" in mods or "relay32.dll" in mods
                except Exception:
                    pass
                gui_log = childlog.latest_injected_log(gui_pid)
            else:
                gui_log = ""
            took_gui = str(gui_pid) in adopted_pids
            print("  [T7] GUI 型后代 pid={}：被接管={} 模块含 injected={} DLL 日志={}".format(
                gui_pid, took_gui, gui_hooked, "有" if gui_log else "无"))
            if took_gui or gui_hooked:
                print("       ✗ 回归！GUI 型后代被接管（输入路由会被它抢走）")
            else:
                print("       ✓ 有可见窗口的后代未被接管")
            verdict["T7_gui_not_adopted"] = not (took_gui or gui_hooked)
            verdict["T7_tui_adopted"] = str(py_pid) in adopted_pids
            print("       TUI pid={} 被接管={}".format(py_pid, verdict["T7_tui_adopted"]))

        # ---- T8：卸载后源终端恢复（用户报告的恢复路径：卸载回原 WT，TUI 恢复正常）
        #  管道断开 → 被接管的后代与目标一起卸载 Hook → 输出回到原 ConHost。
        #  量法：关掉目标终端（mediator 随之退出）→ 源终端应重新收到字节。
        if dst is not None:
            try:
                dst_tap.stop()
            except Exception:
                pass
            try:
                dst.close()
            except Exception:
                pass
            dst = None
            dst_tap = None
            kill_mediators()
            time.sleep(2.5)
            base = src_tap.n()
            time.sleep(3.0)
            back = src_tap.n() - base
            scr = src_tap.screen()
            src_ok = "TaskBoard" in scr
            # 卸载恰好撞上 TUI 重绘帧时可能读到中间态：判据允许一次重试
            for _ in range(2):
                if src_ok:
                    break
                time.sleep(2.0)
                scr = src_tap.screen()
                src_ok = "TaskBoard" in scr
            print("  [T8] 卸载后源终端 3 秒新增字节 = {} → 输出{}；源屏幕{} TUI 画面".format(
                back, "已回到原 ConHost" if back > 0 else "未恢复",
                "仍含" if src_ok else "丢了"))
            if not src_ok:
                # 取证：卸载前后源端字节 + 还原出的屏幕（判断是"真丢了"还是判据被时序干扰）
                tail = bytes(src_tap.buf[max(0, base - 64):base + back])
                print("       [dump] 卸载窗口字节（{}B，前 160B 的 hex）: {}".format(
                    len(tail), " ".join("{:02X}".format(b) for b in tail[:160])))
                print("       [dump] 还原屏幕前 8 行:")
                for ln in scr.split("\n")[:8]:
                    print("       | " + ln)
            verdict["T8_unload_recovery"] = (back > 0, src_ok)

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
        kill_tree(py_pid if target_mode != "shell" else 0)
        kill_tree(shell_pid)
        kill_mediators()

    print("  [verdict] {}".format(verdict))
    return verdict


def calib():
    """标定「按键 → 可观测效果」，避免拿失效判据下结论。

    直接在同一个 ConPTY 里跑 TUI（不注入、不做任何劫持），逐个试验按键，
    记录：进程是否退出 / 屏幕是否变化 / 变化里能否看到该字符本身。
    据此选出无歧义的输入判据，再用于 A/B 组。
    """
    print(SEP)
    print("[校准] 未注入的 ConPTY 里直接跑 TUI，试按键的可观测效果")
    print(SEP)
    src = None
    tap = None
    shell_pid = py_pid = 0
    try:
        src = pywezterm.Pty(COLS, ROWS)
        shell_pid, _ = src.spawn([COMSPEC, "/k", PY, "-u", TASKBOARD])
        tap = Tap(src)
        time.sleep(0.5)
        for c in psutil.Process(shell_pid).children(recursive=True):
            if c.name().lower().startswith("python"):
                py_pid = c.pid
        if not wait_screen_in(tap, "TaskBoard", 30.0):
            print("  [abort] TUI 没画起来")
            return

        def alive():
            try:
                return psutil.pid_exists(py_pid)
            except Exception:
                return False

        steps = [
            ("q", b"q"),
            ("esc+q", b"\x1bq"),
            ("ESC 后 q", b"\x1b\x1bq"),
            ("鼠标点击侧栏(列10,行5)", b"\x1b[<0;10;5M\x1b[<0;10;5m"),
            ("a（新增任务弹窗）", b"a"),
            ("ESC", b"\x1b"),
            ("?", b"?"),
        ]
        for label, seq in steps:
            before = tap.screen()
            src.write(seq)
            time.sleep(2.0)
            after = tap.screen()
            changed = before != after
            has_q = "'q'" in after or "q " in after
            print("  [{:<24}] 退出={} 屏幕变化={} 屏幕出现 'q'={}".format(
                label, not alive(), changed, has_q))
            if not alive():
                print("        → TUI 已退出，后续步骤无效")
                break
        print("  [dumped screen]\n{}".format(
            "\n".join("      | " + ln for ln in tap.screen().split("\n")[:14])))
    finally:
        for x in (tap,):
            try:
                if x:
                    x.stop()
            except Exception:
                pass
        try:
            if src:
                src.close()
        except Exception:
            pass
        kill_tree(shell_pid)
        kill_mediators()


def main():
    if not os.path.exists(MEDIATOR_EXE):
        print("[SKIP] 未找到 {}".format(MEDIATOR_EXE))
        return 0
    if not os.path.exists(TASKBOARD):
        print("[SKIP] 未找到 {}".format(TASKBOARD))
        return 0
    try:
        import textual  # noqa: F401
    except ImportError:
        print("[SKIP] 未安装 textual（pip install textual）")
        return 0

    if "--calib" in sys.argv:
        calib()
        return 0

    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].upper()

    results = {}
    if only in (None, "A"):
        results["A"] = scenario("A", "shell")
    if only in (None, "B"):
        results["B"] = scenario("B", "tui")
    if only in (None, "C"):
        results["C"] = scenario("C", "shell", with_gui=True)

    print(SEP)
    for k, v in results.items():
        if v is None:
            print("[{}] 未完成".format(k))
            continue
        extra = ""
        if "T7_gui_not_adopted" in v:
            extra = " GUI未被接管={} TUI被接管={}".format(
                v["T7_gui_not_adopted"], v["T7_tui_adopted"])
        print("[{}] 重放={} 目标端流量={} 源端流量={} 输入={} Adopt={}{}".format(
            k, v.get("T2_replay"), v.get("T3_dst_flow"), v.get("T4_src_flow"),
            v.get("T5_input"), v.get("T6_adopted"), extra))
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())
