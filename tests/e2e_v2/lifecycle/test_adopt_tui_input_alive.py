"""特性: 接管注入前已存在的 TUI 后，输入不能丢、程序不能自己退出    类别: lifecycle

**针对用户现场报告的回归**（2026-09-27）：
  开 WT → 跑 `tests/live/textual/taskboard.py`（Textual TUI）→ 劫持到新 WT
  → **鼠标、键盘均不可用，但画面正常刷新** → 约 10s 后**程序自己退出**，回到
  `PS C:\\Users\\rikka>`。

拆成四条可判定的断言：

| # | 现场现象 | 本用例的判据 |
|---|---|---|
| 1 | 画面正常刷新 | 注入后目标终端**持续**收到新字节（若后代没被接管，会只停在重放那一帧） |
| 2 | 键盘不可用 | 敲进去的独有标记串必须**出现在 TUI 界面上** |
| 3 | 鼠标不可用 | ① 握手后终端侧要收到鼠标上报启用序列（说明鼠标模式被识别）；② 发一次鼠标按下，mediator 必须把它路由出去 |
| 4 | ~10s 后自己退出 | 注入后 20s，TUI 进程仍在，且屏幕还是 TUI（没有回到 shell 提示符） |

为什么判据用"标记串上屏"而不是"敲 q 让它退出"：
  taskboard 启动后焦点在过滤输入框里，可打印字符会被控件吃掉、方向键也无效 ——
  拿"程序是否退出"当判据会误判成"输入不通"（见 docs/BUGS.md TRAP-009）。
  独有标记串会显示在屏幕上，既不受控件焦点影响，也不会被定时器重绘伪造。

与 `test_adopt_console_descendants` 的分工：
  那条覆盖"接管了谁、跳过了谁"（机制 + GUI 型后代不得被接管），源 shell 用 **cmd**；
  本条覆盖"接管之后输入还通不通、程序会不会自己死"，源 shell 用 **pwsh**（用户的现场就是 pwsh）。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pwcommon import pyterm
from pwcommon import paths
from pwcommon.session import PS7_EXE, PwSession

NAME = "adopt_tui_input_alive"
COLS, ROWS = 120, 30
TUI_SCRIPT = os.path.join(paths.PROJECT_ROOT, "tests", "live", "textual", "taskboard.py")
# taskboard.py 的可见标题（App.TITLE）：判定"屏幕还是 TUI 而不是 shell"
TUI_TITLE = "TaskBoard"
ALIVE_SECONDS = 20.0     # 现场是 ~10s 退出，这里守 20s


def _find_tui_pid(shell_pid: int):
    """在源 shell 的子进程里找跑 TUI 的那个 python。"""
    try:
        import psutil
        for c in psutil.Process(shell_pid).children(recursive=True):
            try:
                if (c.name() or "").lower().startswith("python"):
                    return c.pid
            except Exception:
                pass
    except Exception:
        pass
    return 0


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
    try:
        # 1. 源终端（pwsh）里跑起 TUI —— 注入【之前】就存在，正是回归条件
        # call_prefix="&"：源 shell 是 pwsh，跑带引号的 exe 必须加调用运算符，否则 ParserError
        with PwSession(host="conpty", cols=COLS, rows=ROWS, src_size=(COLS, ROWS),
                       target_args=[PS7_EXE, "-NoLogo"], call_prefix="&") as s:
            s.shell_run([sys.executable, TUI_SCRIPT], lane="src")
            if not s.wait_screen(TUI_TITLE, timeout=45.0, lane="src"):
                print("  [FAIL] 注入前源终端未见 TUI 标题（用例前提不成立）")
                return 1
            print("  [PASS] 注入前 TUI 已在源终端渲染（目标 pid={}）".format(s.target_pid))
            tui_pid = _find_tui_pid(s.target_pid)
            print("  [INFO] TUI 子进程 pid={}".format(tui_pid))

            # 2. 劫持到新终端
            if not s.spawn_mediator():
                print("  [FAIL] 握手失败")
                return 1
            print("  [PASS] 握手成功（mediator pid={}）".format(s.mediator_pid))
            ok, actual = s.assert_topology("conpty", timeout=15.0)
            print("  [INFO] 拓扑={}（期望 conpty）".format(actual))

            # 3. 画面必须**持续**刷新（只停在重放帧 = 后代没被接管）
            time.sleep(2.0)
            buckets = []
            prev = len(s.output())
            for _ in range(5):
                s.drain(1.0)
                buckets.append(len(s.output()) - prev)
                prev = len(s.output())
            if sum(1 for b in buckets if b > 0) >= 4:
                print("  [PASS] 注入后画面持续刷新（每秒新增字节 {}）".format(buckets))
            else:
                print("  [FAIL] 注入后画面定格（每秒新增字节 {}）—— 后代未被接管".format(buckets))
                failures += 1

            # 4. 键盘：独有标记必须上屏
            token = "tk{}".format(int(time.time()) % 100000)
            s.write(token)
            if s.wait_screen(token, timeout=12.0):
                print("  [PASS] 键盘输入到达（屏幕上出现 {}）".format(token))
            else:
                print("  [FAIL] 键盘输入未到达（{} 未上屏）；屏幕尾部={}".format(
                    token, s.tail_lines(6)))
                failures += 1

            # 5. 鼠标：① 握手后终端侧应收到鼠标上报启用序列（说明识别到鼠标模式）
            if s.wait_bytes(b"\x1b[?1002h", timeout=12.0):
                print("  [PASS] 终端侧收到鼠标上报启用序列 ?1002h")
            else:
                print("  [FAIL] 终端侧未见 ?1002h（鼠标模式未被识别/未补发）")
                failures += 1
            #    ② 发一次鼠标按下，mediator 必须把它路由出去
            s.mouse(COLS // 2, ROWS // 2, kind="press", button="left")
            routed = s.wait_log_regex(r"RouteInput", timeout=8.0, which="mediator")
            if routed:
                print("  [PASS] 鼠标事件经 mediator 路由（日志命中 RouteInput）")
            else:
                print("  [FAIL] 鼠标事件未被路由（mediator 日志无 RouteInput 增量）")
                failures += 1

            # 6. 不能自己退出：20s 后 TUI 仍在，且屏幕还是 TUI
            deadline = time.time() + ALIVE_SECONDS
            died_at = None
            while time.time() < deadline:
                if tui_pid and not _alive(tui_pid):
                    died_at = ALIVE_SECONDS - (deadline - time.time())
                    break
                if s.wait_screen("PS ", timeout=1.0) and TUI_TITLE not in s.text():
                    died_at = ALIVE_SECONDS - (deadline - time.time())
                    break
                time.sleep(0.5)
            if died_at is None:
                print("  [PASS] 注入后 {}s 内 TUI 未自行退出（进程在、屏幕仍是 TUI）".format(
                    int(ALIVE_SECONDS)))
            else:
                print("  [FAIL] 注入后约 {:.0f}s 时 TUI 自行退出（复现用户现场）；"
                      "屏幕尾部={}".format(died_at, s.tail_lines(6)))
                failures += 1
    except (RuntimeError, KeyError) as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


def _alive(pid: int) -> bool:
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(run())
