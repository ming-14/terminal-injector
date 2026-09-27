"""特性: ConPTY 托管的 **cmd** 的注入（"WT 里开的 cmd"）    类别: lifecycle

链路: 目标 cmd 跑在 pywezterm.Pty 里（= WT 里开的 cmd 的形态）
        ←注入── mediator 跑在另一个 Pty 里（= 新终端）

为什么单列一个用例（而不是并入 conpty_hosted_target）:
  v1 的 `test_conpty_hosted_target` 用的是 **pwsh**，本用例用 **cmd** ——
  两者的关键行为不同：ConPTY 托管的 pwsh **不重印 prompt**（所以才有了
  `promptOverwrite = isLineShell && !conptyHosted` 这条判据，BUG-013b），
  而 **cmd 在子进程退出后会重印 prompt**。也就是说"conptyHosted=1 但程序会重印
  prompt"这个组合此前从未被覆盖过（opentui 那次 prompt 重复就是踩在这个组合上，
  见 docs/BUGS.md BUG-023）。

  更实际的一面：绝大多数用户的注入目标就是"WT 里开的 cmd"，而 v1 里除了上面那条
  pwsh 用例，其余全跑在经典 ConHost（`conptyHosted=0`）上 —— 覆盖率是偏的。

"真 WT 是必需的吗" —— 不是。判据见下：
  pywezterm 侧载的 OpenConsole.exe 提供的是**同一个 ConPTY**，目标进程的控制台
  窗口类名同样是 `PseudoConsoleWindow`，DLL 的 `IsConPtyHosted()` 判出来的结果
  与真 WT 一致（本用例断言 `conptyHosted=1` 就是证据）。
  真 WT 只在"WT 自身的行为差异"上不可替代（渲染/UIA、以及 WT 支持而 ConPTY
  不支持的协议 —— 例如 BUG-022 的 `>1u` 只在真 WT 崩）。

预期:
  - 拓扑自证：`conptyHosted=1`（不是经典 ConHost 的 0）
  - 按行编辑 shell 处理（`lineShell=1`），不被误判成全屏 TUI
  - 敲 echo 后屏幕出现输出行；键入的独有标记能上屏（输入可达）
  - 提示符行正常回显（cmd 的 prompt 在注入后可见）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pwcommon import pyterm
from pwcommon.session import COMSPEC, PwSession

NAME = "conpty_hosted_cmd"
COLS, ROWS = 120, 30
TEXT = "TI_WTCMD"


def run() -> int:
    pyterm.require()
    failures = 0

    try:
        # 目标 = 跑在 Pty 里的 cmd（"WT 里开的 cmd"），mediator = 另一个 Pty（"新终端"）
        with PwSession(host="conpty", cols=COLS, rows=ROWS,
                       src_size=(COLS, ROWS), target_args=[COMSPEC]) as s:
            if not s.spawn_mediator():
                print("  [FAIL] 握手失败")
                print("\nSUMMARY: FAIL (1 failures)")
                return 1
            print("  [PASS] 握手成功（目标 pid={}）".format(s.target_pid))

            # 1. 拓扑自证：必须是 ConPTY 托管，否则本用例覆盖的就不是这条路径
            ok, actual = s.assert_topology("conpty", timeout=15.0)
            if ok:
                print("  [PASS] 拓扑自证 conptyHosted=1（WT 里开的 cmd 形态）")
            else:
                print("  [FAIL] 拓扑不符：期望 conpty，实际 {} —— "
                      "本用例会静默覆盖到另一条分支".format(actual))
                failures += 1

            # 2. 按行编辑 shell 处理（未被误判为全屏 TUI）
            if s.wait_log("lineShell=1", timeout=10.0, which="dll"):
                print("  [PASS] lineShell=1（按行编辑 shell 处理）")
            else:
                print("  [FAIL] DLL 日志未见 lineShell=1")
                failures += 1

            # 3. 屏幕上出现输出行（整行相等，排除命令回显的假命中）
            s.send_line("echo {}".format(TEXT))
            if s.wait_line(TEXT, timeout=10.0):
                print("  [PASS] 屏幕上出现输出行 {}".format(TEXT))
            else:
                print("  [FAIL] 屏幕上没有输出行 {}；屏幕尾部={}".format(
                    TEXT, s.tail_lines(6)))
                failures += 1

            # 4. 输入可达：独有标记必须上屏
            token = "tok{}".format(int(time.time()) % 100000)
            s.send_line("echo {}".format(token))
            if s.wait_line(token, timeout=10.0):
                print("  [PASS] 键入 {} 后上屏（输入经 mediator→DLL 到达目标）".format(token))
            else:
                print("  [FAIL] 键入 {} 未上屏；屏幕尾部={}".format(token, s.tail_lines(6)))
                failures += 1

            # 5. cmd 的提示符行在注入后可见
            if s.wait_line("terminal-injector>", timeout=10.0, exact=False):
                print("  [PASS] 提示符行可见")
            else:
                print("  [FAIL] 未见提示符行；屏幕尾部={}".format(s.tail_lines(6)))
                failures += 1
    except (RuntimeError, KeyError) as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
