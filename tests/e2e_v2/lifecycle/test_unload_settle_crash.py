"""特性: 卸载完成后，源 shell 再发生一次终端 I/O 不能崩    类别: lifecycle

**针对 BUG-031 的根因回归**（2026-09-29 cdb 取证）：
  旧终端 → 劫持到新终端 → 新终端跑 TUI → 卸载 → 回旧终端操作 →
  pwsh 崩溃 `0xc0000005`（现场退出码 3221225477）。

根因（cdb 全线程栈实证，见 `docs/report/2026-09-29-unload-crash-0xc0000005-report.md`）：
  - 目标是 pwsh，其输入线程阻塞在 **`WaitForMultipleObjectsEx_Detour`** 内部
    （`call r10` → kernelbase → `NtWaitForMultipleObjects`）。
  - `Unloader::DoUnload` 第 5.5 步只等 **Read 类 detour** 退出
    （`ActiveReadDetours()` 计数，靠 `ReadDetourGuard`）。
    `WaitHooks.cpp` 的 detour 只用了 `HookReentryGuard`，**不在计数内**。
  - 于是等待被"已经归零"骗过 → 助手远程 `FreeLibrary` → `injected.dll` 卸载。
  - 该线程从等待返回时要把控制权弹回 `injected.dll+0x3011b`（detour 返回点），
    那段代码已不存在 → AV。cdb 标注为 `<Unloaded_injected.dll>+0x3011b`。

**本用例的关键判据 = 时间**：
  实测崩溃是 0/1 硬阈值，卡在"卸载完成 → 首次终端 I/O"**3.0 秒**：
  settle ≤ 2.9s 全绿、≥ 3.2s 全崩（`tests/_probe/scan_unload_settle.py`）。
  既有的 `test_unload_tui_shell_alive` 只 sleep 2.0s 就 echo ⇒ **恰好落在
  阈值以内**，所以它测不到这个缺陷（无论 TUI 死活）。本用例显式跨过阈值。

场景（**两条都必须过**；都只断"不该崩"）：
  A：TUI 正常退出后卸载 —— 现有用例判 PASS，实为漏测
  B：TUI 仍在运行时卸载 —— 现有用例判 FAIL

每个场景的断言：
  1. 卸载后等待 ≥ SETTLE（跨过阈值）
  2. 源 shell 仍活着
  3. 注入 Enter（空行，现场描述的按键）后仍活着
  4. 注入 `echo <token>` + 回车后仍活着，且输出真的回显（不只是"没崩"）
  5. 源终端屏幕不含 TUI 标题（会话内容未被重放回源终端 —— BUG-031 的另一半）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pwcommon import pyterm
from pwcommon import paths
from pwcommon.session import PS7_EXE, PwSession

NAME = "unload_settle_crash"
COLS, ROWS = 120, 30
TUI_SCRIPT = os.path.join(paths.PROJECT_ROOT, "tests", "live", "textual", "taskboard.py")
TUI_TITLE = "TaskBoard"

# 卸载完成 → 首次终端 I/O 的等待秒数。
# 实测阈值 3.0s（≤2.9 全绿 / ≥3.2 全崩），取 3.5s 留出余量。
SETTLE = 3.5

SCENARIOS = (
    {"name": "TUI 正常退出后卸载", "quit_tui": True},
    {"name": "TUI 仍在运行时卸载", "quit_tui": False},
)


def _alive(pid: int) -> bool:
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


def _run_one(sc) -> int:
    failures = 0
    with PwSession(host="conpty", cols=COLS, rows=ROWS, src_size=(COLS, ROWS),
                   target_args=[PS7_EXE, "-NoLogo"], call_prefix="&") as s:
        shell_pid = s.target_pid
        if not s.spawn_mediator():
            print("  [FAIL] 握手失败")
            return 1
        print("  [PASS] 握手成功（源 shell pid={}）".format(shell_pid))

        # 1. 在**新终端**里跑 TUI（注入之后启动 ⇒ 走 CreateProcess 捕获路径）
        s.shell_run([sys.executable, TUI_SCRIPT], lane="dst")
        if not s.wait_screen(TUI_TITLE, timeout=45.0, lane="dst"):
            print("  [FAIL] 新终端未见 TUI 标题（用例前提不成立）")
            return 1
        print("  [PASS] TUI 在新终端渲染（TaskBoard 可见）")

        # 2. 场景 A 才退出 TUI
        if sc["quit_tui"]:
            # 焦点在过滤输入框里，q 会被吃掉；Tab 移开焦点后 q 才生效
            s.press("Tab", lane="dst")
            time.sleep(0.8)
            s.write("q", lane="dst")
            deadline = time.time() + 20.0
            while time.time() < deadline and TUI_TITLE in s.text("dst"):
                time.sleep(0.3)
            if TUI_TITLE in s.text("dst"):
                print("  [FAIL] Tab+q 后 TUI 未退出（前提不成立）")
                return 1
            print("  [PASS] TUI 已退出")
            s.wait_stable(quiet=1.0, timeout=8.0, lane="dst")
        else:
            print("  [INFO] TUI 保持运行，直接卸载")

        # 3. 卸载
        s.close_mediator()
        print("  [PASS] 已卸载（关闭 mediator）")

        # 4. ★ 关键：等足够久，跨过"卸载完成 → 首次 I/O"的 3 秒阈值。
        #    不这样做就永远落在安全窗口内 —— 这正是旧用例漏测的原因。
        print("  [INFO] 等 {:.1f}s 跨过崩溃阈值（实测 3.0s）...".format(SETTLE))
        time.sleep(SETTLE)

        if not _alive(shell_pid):
            print("  [FAIL] 等待期间源 shell 已崩（未等到输入就死了）")
            return 1
        print("  [PASS] 卸载 + {:.1f}s 后源 shell 仍存活".format(SETTLE))

        # 5. 现场描述的按键：Enter（空行提交）
        s.press("Enter", lane="src")
        time.sleep(1.5)
        if not _alive(shell_pid):
            print("  [FAIL] 卸载后按 Enter 触发源 shell 崩溃（复现现场 0xc0000005）")
            return 1
        print("  [PASS] 按 Enter 后源 shell 仍存活")

        # 6. 真实终端 I/O：echo + 回车，且要看到回显（不能只断"没崩"）
        token = "us{}".format(int(time.time()) % 100000)
        s.send_line("echo {}".format(token), lane="src")
        time.sleep(1.5)
        if not _alive(shell_pid):
            print("  [FAIL] 卸载后执行命令触发源 shell 崩溃（复现现场 0xc0000005）")
            return 1
        print("  [PASS] 执行命令后源 shell 仍存活")

        if s.wait_line(token, timeout=12.0, lane="src", exact=True):
            print("  [PASS] 命令真的执行了（屏幕出现 {}）".format(token))
        else:
            print("  [FAIL] 命令无回显；屏幕尾部={}".format(s.tail_lines(4, lane="src")))
            failures += 1

        # 7. 会话内容不该被重放回源终端（BUG-031 的另一半）
        if TUI_TITLE not in s.text("src"):
            print("  [PASS] 源终端无 TUI 画面残留")
        else:
            print("  [FAIL] 源终端出现 TUI 画面 —— 会话内容被重放回旧终端；"
                  "尾部={}".format(s.tail_lines(3, lane="src")))
            failures += 1

    return failures


def run() -> int:
    pyterm.require()
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
    for sc in SCENARIOS:
        print("\n── 场景：{} ──".format(sc["name"]))
        failures += _run_one(sc)

    print("\nSUMMARY: {} ({} failures, {} checks)".format(
        "PASS" if failures == 0 else "FAIL", failures, len(SCENARIOS) * 7))
    return failures


if __name__ == "__main__":
    sys.exit(run())
