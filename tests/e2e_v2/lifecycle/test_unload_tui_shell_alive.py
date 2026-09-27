"""特性: 跑过 TUI 后卸载，源 shell 不能崩、会话内容不能回灌旧终端    类别: lifecycle

**针对用户现场报告的回归**（2026-09-28）：
  开 WT → 劫持到新 WT → 在**新** WT 里跑 `tests/live/textual/taskboard.py`
  → 退出 TUI → 卸载、回到旧 WT → 随便敲点东西 → **pwsh 崩溃**
  （退出码 3221225477 = `0xc0000005` 访问违例）。

  现场输出里还有一条关键线索：回到旧终端后，屏幕上出现了 **TUI 的菜单文本**
  （`1 是刚打开的文件`、`2 一可回滚的资源` 之类），像是会话期内容被重放回了旧终端、
  甚至被当成了输入。所以本用例不只断"崩不崩"，还断"会话内容有没有回灌"。

两个场景（都是 bug，都断言"不该崩"）：

| 场景 | 现状 |
|---|---|
| A：TUI **正常退出**后卸载 | 当前未复现（PASS） |
| B：TUI **仍在运行**时卸载 | **复现**（FAIL）—— 源终端出现 TUI 画面 + 第 1 次交互崩溃 |

  场景 B 很可能就是用户的真实情况：taskboard 启动后焦点在过滤输入框里，
  **单独发 `q` 会被输入框吃掉**（实测 q 与 ctrl-c 都无效，Tab 移开焦点后 q 才生效）
  ⇒ 用户以为 TUI 退出了，实际进程还在 ⇒ 卸载时它还活着。

每条场景断言（卸载之后，全部在源终端上验）：
  1. 源 shell 进程仍在（没崩）
  2. 源 shell 仍能响应（敲 `echo <标记>` 能回显）
  3. 源终端屏幕**不含** TUI 标题 —— TUI 是在**新**终端里跑的，画面不该被重放回旧终端
  4. 连续敲若干条命令后仍存活（对应现场"随便干一点什么就崩"）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pwcommon import pyterm
from pwcommon import paths
from pwcommon.session import PS7_EXE, PwSession

NAME = "unload_tui_shell_alive"
COLS, ROWS = 120, 30
TUI_SCRIPT = os.path.join(paths.PROJECT_ROOT, "tests", "live", "textual", "taskboard.py")
TUI_TITLE = "TaskBoard"
NUISANCE_ROUNDS = 6     # 现场"随便干一点什么"就崩，这里敲够次数

SCENARIOS = (
    {"name": "TUI 正常退出后卸载", "quit_tui": True},
    {"name": "TUI 仍在运行时卸载（现场复现）", "quit_tui": False},
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
    try:
        # 目标 = pwsh（跑在源终端里），劫持后在新终端里跑 TUI
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

            # 2. 场景 A 才退出 TUI；场景 B 故意让它继续跑着
            if sc["quit_tui"]:
                # 注意：不能直接发 q —— 焦点在过滤输入框里会被吃掉。
                # 实测 q / ctrl-c 都无效，Tab 移开焦点后 q 才生效。
                s.press("Tab", lane="dst")
                time.sleep(0.8)
                s.write("q", lane="dst")
                deadline = time.time() + 15.0
                while time.time() < deadline and TUI_TITLE in s.text("dst"):
                    time.sleep(0.3)
                if TUI_TITLE in s.text("dst"):
                    print("  [FAIL] Tab+q 后 TUI 未退出（前提不成立，后面不算数）")
                    return 1
                print("  [PASS] TUI 已退出（Tab 移开焦点后 q 生效）")
                s.wait_stable(quiet=1.0, timeout=6.0, lane="dst")
            else:
                print("  [INFO] 场景 B：TUI 保持运行，直接卸载")

            # 3. 卸载（关掉新终端 → 管道断开 → DLL 卸载）
            s.close_mediator()
            time.sleep(2.0)
            print("  [PASS] 已卸载（关闭 mediator）")

            # 4. 源 shell 进程必须还在
            if _alive(shell_pid):
                print("  [PASS] 卸载后源 shell 仍在（未崩溃）")
            else:
                print("  [FAIL] 卸载后源 shell 已消失（复现现场的崩溃）")
                return 1

            # 5. 源 shell 仍要能响应
            token = "ul{}".format(int(time.time()) % 100000)
            s.send_line("echo {}".format(token), lane="src")
            if s.wait_line(token, timeout=12.0, lane="src"):
                print("  [PASS] 源 shell 仍响应（屏幕上出现 {}）".format(token))
            else:
                print("  [FAIL] 源 shell 无响应；屏幕尾部={}".format(s.tail_lines(4, lane="src")))
                failures += 1

            # 6. 会话内容不该被重放回旧终端
            if TUI_TITLE not in s.text("src"):
                print("  [PASS] 源终端无 TUI 画面残留（会话内容未被回灌）")
            else:
                print("  [FAIL] 源终端出现 TUI 画面 —— 会话内容被重放回旧终端；"
                      "屏幕尾部={}".format(s.tail_lines(3, lane="src")))
                failures += 1

            # 7. "随便干一点什么"：连续敲若干条，仍要活着
            died_at = None
            for i in range(NUISANCE_ROUNDS):
                s.send_line("echo n{}".format(i), lane="src")
                time.sleep(0.6)
                if not _alive(shell_pid):
                    died_at = i + 1
                    break
            if died_at is None:
                print("  [PASS] 连续 {} 次交互后源 shell 仍存活".format(NUISANCE_ROUNDS))
            else:
                print("  [FAIL] 卸载后第 {} 次交互时源 shell 崩溃（复现现场）".format(died_at))
                failures += 1
    except (RuntimeError, KeyError) as e:
        print("  [FAIL] setup 失败: {}".format(e))
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

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
