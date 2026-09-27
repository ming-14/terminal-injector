"""特性: 接管「注入前已存在的同控制台后代」    类别: lifecycle

链路: 源终端（真 ConPTY，扮演 WT）= cmd shell
        ├─ python taskboard.py（Textual TUI，**注入前就在跑**，屏幕占用者）
        └─ python gui_like.py（有可见顶层窗口、完全不碰控制台的 GUI 型后代）
        ←注入── mediator（跑在另一个 ConPTY 里，扮演新 WT）

预期:
  - 注入时枚举同控制台进程，把「父链能回溯到注入目标 + 确实在画这块终端」的后代接管
  - 被接管的 TUI：注入后目标终端**持续**收到新输出（不是只停在重放的那一帧）
  - 被接管的 TUI：目标终端敲进去的字符出现在它自己的界面上（输入真的到达）
  - **GUI 型后代不得被接管**：它有可见顶层窗口、既不画终端也不读终端；
    接管它只会抢走输入路由，还会连它的 CreateProcess 一起 Hook
    （它再拉起 wt.exe 就会被顺带注入）

回归背景（2026-09-27 用户报告两次，一次是 bug、一次是修法的回归）:
  1) 用户：开 WT → 在里面跑 taskboard.py → 把承载它的 shell 劫持到新 WT：
     新 WT 上 TUI 画面定格、按键无效。根因：注入只接管「目标本身」+「注入之后
     由它 CreateProcess 出的子进程」，注入前已在运行的后代永远不被接管 ——
     它的输出仍写旧 ConHost，新 WT 只停在注入瞬间重放的那一帧。
     （对照：直接劫持 TUI 自身（python.exe）是正常的，所以断言必须区分这两种目标。）
  2) 首版修法「无条件接管同控制台的全部后代」被回滚，因为**接管了用户自己的
     tkinter 管理界面**（从同一个 shell 里启动，因而同控制台、也是目标的后代）：
     输入被路由给一个根本不读控制台的进程 → 无法输入；它的 CreateProcess 也被 Hook。
     故本用例同时断言「GUI 型后代不被接管」。

为什么用真 Textual TUI 而不是计时器脚本:
  数据要贴近真实分布 —— 屏幕占用者是**全屏 TUI**（alt buffer / raw 输入 / 持续重绘），
  与行编辑 shell 的 I/O 形态完全不同；用计时器会把「TUI 能不能被接管」这个问题掩盖掉。
  TUI 脚本用仓库内的实测样例 tests/live/textual/taskboard.py（未装 textual 记 UNSUPPORTED）。

为什么用 pywezterm 的 ConPTY 而不是真 WT:
  同样是 ConPTY（目标控制台窗口类名 = PseudoConsoleWindow，与 WT 一致），
  但能直接读字节断言、零焦点依赖、秒级完成。未装 pywezterm 时记 UNSUPPORTED。

验证方式: 目标终端字节（重放帧 / 注入后持续新输出 / 键入标记上屏）
          + DLL 日志（Adopt 接管与 skip 明细）+ 活模块表（GUI 进程里没有 injected.dll）
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.paths import PROJECT_ROOT, BUILD_BIN, RESULTS_DIR, ti_log_path
from common import childlog

NAME = "adopt_console_descendants"

PWTERM_DIR = os.environ.get("PWTERM_DIR") or os.path.join(PROJECT_ROOT, "reference")
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
COMSPEC = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "cmd.exe")
TUI_SCRIPT = os.path.join(PROJECT_ROOT, "tests", "live", "textual", "taskboard.py")
PY = sys.executable
COLS, ROWS = 120, 30

# TUI 的可见标题（taskboard.py 的 App.TITLE）—— 重放帧/画面刷新判据
TUI_TITLE = "TaskBoard"

# GUI 型后代：有可见顶层窗口、完全不碰控制台。
# 窗口放到屏幕外，避免测试时打扰桌面；IsWindowVisible 仍为真。
GUI_LIKE_SRC = (
    "import tkinter as tk\n"
    "root = tk.Tk()\n"
    "root.title('ti-e2e-gui-like')\n"
    "root.geometry('120x80+4000+4000')\n"
    "root.after(120000, root.destroy)\n"
    "root.mainloop()\n"
)

# 源终端启动脚本：先起 GUI 型后代（后台、同控制台），再起屏幕占用者 TUI
SH_SRC = (
    "@echo off\n"
    'start "" /b "{py}" -u "{gui}"\n'
    '"{py}" -u "{tui}"\n'
)


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _pump(pty, seconds, interval=0.1):
    """读 pty 至多 seconds 秒，返回累计字节（不读会把 ConPTY 写端堵死）。"""
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


def _wait_screen(pty, term, needle, timeout, acc=b""):
    """持续读+喂终端，直到可见屏幕出现 needle。返回 (命中, 累计字节)。"""
    buf = acc
    end = time.time() + timeout
    while time.time() < end:
        if needle in term.text():
            return True, buf
        buf += _pump(pty, 0.25)
        if buf:
            try:
                term.feed(bytes(buf))
            except Exception:
                pass
    return needle in term.text(), buf


def _module_names(pid):
    try:
        import psutil
        return {os.path.basename(m.path).lower()
                for m in psutil.Process(pid).memory_maps()}
    except Exception:
        return set()


def run() -> int:
    if PWTERM_DIR not in sys.path:
        sys.path.insert(0, PWTERM_DIR)
    try:
        import pywezterm
        import psutil
    except ImportError as e:
        print("  [SKIP] 无法 import pywezterm/psutil (PWTERM_DIR={!r}): {}".format(
            PWTERM_DIR, e))
        print("\nSUMMARY: UNSUPPORTED (pywezterm/psutil 不可用)")
        return 0
    try:
        import textual  # noqa: F401
    except ImportError:
        print("  [SKIP] 未安装 textual（pip install textual）")
        print("\nSUMMARY: UNSUPPORTED (textual 不可用)")
        return 0
    if not os.path.exists(TUI_SCRIPT):
        print("  [SKIP] 缺少 TUI 脚本 {}".format(TUI_SCRIPT))
        print("\nSUMMARY: UNSUPPORTED (缺少 TUI 实测样例)")
        return 0

    failures = 0
    src = dst = None
    shell_pid = target_pid = tui_pid = gui_pid = 0
    try:
        os.makedirs(RESULTS_DIR, exist_ok=True)
        gui_script = os.path.join(RESULTS_DIR, "adopt_gui_like.py")
        with open(gui_script, "w", encoding="utf-8") as f:
            f.write(GUI_LIKE_SRC)
        sh_script = os.path.join(RESULTS_DIR, "adopt_source.cmd")
        with open(sh_script, "w", encoding="ascii") as f:
            f.write(SH_SRC.format(py=PY, gui=gui_script, tui=TUI_SCRIPT))

        # 1. 源终端：shell 里先跑起 TUI（注入【之前】就存在 —— 这正是回归条件）
        src = pywezterm.Pty(COLS, ROWS)
        src_term = pywezterm.Terminal(COLS, ROWS)
        shell_pid, _ = src.spawn([COMSPEC, "/k", sh_script])
        print("  [INFO] 源 shell pid={}（ConPTY 托管；同控制台还有 GUI 型后代）".format(
            shell_pid))
        time.sleep(1.5)
        for c in psutil.Process(shell_pid).children(recursive=True):
            try:
                cl = " ".join(c.cmdline())
            except Exception:
                cl = ""
            if "adopt_gui_like" in cl:
                gui_pid = c.pid
            elif c.name().lower().startswith("python"):
                tui_pid = c.pid
        print("  [INFO] TUI pid={}  GUI 型后代 pid={}".format(tui_pid, gui_pid))

        ok, src_bytes = _wait_screen(src, src_term, TUI_TITLE, 40.0)
        if not ok:
            print("  [FAIL] 注入前源终端未见 TUI 标题（用例前提不成立）：{!r}".format(
                bytes(src_bytes[-200:])))
            print("\nSUMMARY: FAIL ({} failures)".format(failures + 1))
            return failures + 1
        print("  [PASS] 注入前 TUI 已在源终端渲染（{} 字节）".format(len(src_bytes)))
        target_pid = shell_pid

        # 2. 目标终端：mediator 跑在另一个 ConPTY 里（= GUI 的「在新 WT 中劫持」）
        dst = pywezterm.Pty(COLS, ROWS)
        dst_term = pywezterm.Terminal(COLS, ROWS)
        dst.spawn([MEDIATOR_EXE, "--mediator", "--target-pid", str(target_pid)])
        mlog = ti_log_path(target_pid)
        deadline = time.time() + 30
        while time.time() < deadline and "Handshake OK" not in _read(mlog):
            time.sleep(0.3)
        if "Handshake OK" in _read(mlog):
            print("  [PASS] 握手成功（目标 shell pid={}）".format(target_pid))
        else:
            print("  [FAIL] 握手失败（见 {}）".format(mlog))
            failures += 1

        # 3. 重放帧：注入瞬间把源 ConHost 屏幕搬到新终端
        hit, dst_bytes = _wait_screen(dst, dst_term, TUI_TITLE, 15.0)
        if hit:
            print("  [PASS] 目标终端收到重放帧（画面含 {}）".format(TUI_TITLE))
        else:
            print("  [FAIL] 目标终端未见重放帧：{!r}".format(bytes(dst_bytes[-200:])))
            failures += 1

        # 4. 关键断言一：注入后画面**持续**刷新（只停在重放帧 = 后代没被接管）
        time.sleep(3.0)  # 先让重放/首帧落定，再量稳态是否还在动
        buckets = []
        prev = len(dst_bytes)
        for _ in range(5):
            dst_bytes += _pump(dst, 1.0)
            buckets.append(len(dst_bytes) - prev)
            prev = len(dst_bytes)
        flowing = sum(1 for b in buckets if b > 0)
        if flowing >= 4:
            print("  [PASS] 注入后目标终端持续收到新输出（每秒新增字节 {}）".format(buckets))
        else:
            print("  [FAIL] 注入后画面定格（每秒新增字节 {}）—— 屏幕占用者未被接管".format(
                buckets))
            failures += 1

        # 5. 关键断言二：输入真的到达 TUI
        #    判据标定（tests/_probe/t_hijack_order_probe.py --calib）：taskboard 启动后
        #    焦点在 #filter 输入框里，可打印字符会**显示在屏幕上**，故用独有标记串判定：
        #    既不受控件焦点影响，也不会被定时器重绘伪造。
        token = "ti{}".format(int(time.time()) % 100000)
        dst.write(token.encode("utf-8"))
        got_input = False
        end = time.time() + 10.0
        while time.time() < end:
            dst_bytes += _pump(dst, 0.5)
            try:
                dst_term.feed(bytes(dst_bytes))
            except Exception:
                pass
            if token in dst_term.text():
                got_input = True
                break
        if got_input:
            print("  [PASS] 目标终端键入 {!r} 后出现在 TUI 界面上（输入到达）".format(token))
        else:
            print("  [FAIL] 键入 {!r} 未出现在目标终端上（输入未到达 TUI）".format(token))
            failures += 1

        # 6. 机制判据 + 回归判据：接管了谁、跳过了谁
        dll_text = _read(childlog.latest_injected_log(target_pid))
        if not dll_text:
            print("  [FAIL] 未找到目标 DLL 日志（pid={}）".format(target_pid))
            failures += 1
        else:
            adopted = re.findall(r"Adopt: pid=(\d+) depth=(\d+) injected=(\d+)",
                                 dll_text)
            adopted_pids = {a[0] for a in adopted}
            if str(tui_pid) in adopted_pids:
                print("  [PASS] 屏幕占用者 TUI pid={} 已被接管（明细 {}）".format(
                    tui_pid, adopted))
            else:
                print("  [FAIL] TUI pid={} 未被接管（明细 {}）".format(tui_pid, adopted))
                failures += 1

            # GUI 型后代不得被接管：日志里应有 skip 记录
            if "Adopt: skip pid={}".format(gui_pid) in dll_text:
                print("  [PASS] GUI 型后代 pid={} 被显式跳过".format(gui_pid))
            else:
                print("  [FAIL] DLL 日志未见跳过 GUI 型后代 pid={} 的记录".format(gui_pid))
                failures += 1

            # 硬证据：GUI 进程里不能有我们的模块（日志可能被 pid 复用污染，看活模块表）
            mods = _module_names(gui_pid)
            if "injected.dll" in mods or "relay32.dll" in mods:
                print("  [FAIL] GUI 型后代进程里加载了我们的 DLL（模块表含 {}）".format(
                    sorted(m for m in mods if "inject" in m or "relay32" in m)))
                failures += 1
            else:
                print("  [PASS] GUI 型后代进程里没有我们的 DLL（活模块表核对）")

            # mediator 侧：为被接管的后代建了子会话，且只建了一个
            mtext = _read(mlog)
            n_child = mtext.count("ChildSession started")
            if n_child == 1:
                print("  [PASS] mediator 恰好建立了 1 个子会话（只接管屏幕占用者）")
            else:
                print("  [FAIL] mediator 子会话数 = {}（应为 1）".format(n_child))
                failures += 1
    finally:
        for p in (dst, src):
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
            if shell_pid:
                try:
                    proc = psutil.Process(shell_pid)
                    for child in proc.children(recursive=True):
                        try:
                            child.terminate()
                        except Exception:
                            pass
                    proc.terminate()
                except Exception:
                    pass
        except Exception:
            pass

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
