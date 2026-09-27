"""特性: ConPTY 托管目标的注入重放（v2 试点，host="conpty" 拓扑）    类别: lifecycle

链路: 目标 shell 跑在源终端（pywezterm.Pty，150x40）
        ←注入── mediator 跑在另一个 pty 里（扮演新终端，120x30）

预期:
  - 识别出"控制台由 ConPTY 托管"（DLL 日志 conptyHosted=1）
  - 按行编辑 shell 处理（lineShell=1），不被误判为全屏 TUI
  - 注入前的屏幕内容被重放到目标终端（标记可见），且**不是**走 TUI 的
    「跳过屏幕重放」分支、重放字节数 > 4
  - 复现条件自证：源尺寸 != 目标尺寸
  - **目标终端光标与源终端一致**（不得被"行首覆盖"拉到 prompt 行首：
    ConPTY 托管的目标不会重印 prompt 来消费那个位置）

为什么要单独一个用例（而不是并入上面那个）:
  `LazyInit::IsConPtyHosted()` 会把注入分成两条分支。目标跑在 Pty 里就是
  conptyHosted=1，走的是流式 shell 那条路；它和 conhost 拓扑不是同一条代码路径，
  必须各测一遍。

与 v1 的差别:
  - v1 用 pyte 把字节还原成屏幕/光标；v2 用 pywezterm.Terminal（wezterm-term 本体，
    与 WT 同源），语义基准更贴近真实终端。
  - 光标比较改为直接读两个通道的 Terminal 光标（不再自己算 1-based 换算）。
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pwcommon import pyterm
from pwcommon.session import PS7_EXE, PwSession

NAME = "conpty_hosted_target"
MARKER = "TI_CONPTY_MARK"


def run() -> int:
    pyterm.require()

    failures = 0
    try:
        with PwSession(host="conpty", cols=120, rows=30, src_size=(150, 40),
                       target_args=[PS7_EXE, "-NoLogo"]) as s:
            # 1. 源终端：让目标 shell 先跑起来并打印唯一标记
            s.drain(4.0, lane="src")
            s.write("echo {}\r".format(MARKER), lane="src")
            if not s.wait_bytes(MARKER.encode(), timeout=12.0, lane="src"):
                print("  [FAIL] 目标未回显标记（源 ConPTY 里的 shell 未就绪）：{!r}".format(
                    s.output("src")[-200:]))
                print("\nSUMMARY: FAIL (1 failures)")
                return 1
            print("  [PASS] 目标已在源 ConPTY 里打印标记 {}（目标 pid={}）".format(
                MARKER, s.target_pid))
            # 注入时刻的源终端状态 —— 目标终端的光标期望值取这里
            # （不硬编码 prompt 宽度：用户自定义 prompt 也不会让断言失效）
            src_cursor = s.cursor("src")

            # 2. 目标终端：mediator 跑在另一个 pty 里（扮演"新终端"）
            if s.spawn_mediator():
                print("  [PASS] 握手成功（mediator pid={}）".format(s.mediator_pid))
            else:
                print("  [FAIL] 握手失败")
                failures += 1

            # 3. 注入前的屏幕内容必须被重放到目标终端
            if s.wait_bytes(MARKER.encode(), timeout=15.0):
                print("  [PASS] 注入前的屏幕内容已重放到目标终端（字节含 {}）".format(MARKER))
            else:
                print("  [FAIL] 目标终端未见 {} 字节 —— 屏幕未被重放；实收 {} 字节: {!r}".format(
                    MARKER, len(s.output()), s.output()[-200:]))
                failures += 1

            # 4. DLL 侧判据：走了哪条分支
            dll = s.dll_log()
            if not dll:
                print("  [FAIL] 未找到目标 DLL 日志（pid={}）".format(s.target_pid))
                failures += 1
            else:
                for needle, desc, want in (
                        ("conptyHosted=1", "控制台由 ConPTY 托管", True),
                        ("lineShell=1", "按行编辑 shell 处理（未被误判为 TUI）", True),
                        ("skip screen replay", "TUI 的「跳过屏幕重放」分支", False)):
                    got = needle in dll
                    if got == want:
                        print("  [PASS] {}（{}）".format(desc, needle))
                    else:
                        print("  [FAIL] {} 判据不符：{} {}".format(
                            desc, needle, "应出现却没有" if want else "不该出现却出现"))
                        failures += 1

                m = re.search(r"screen content replayed to WT, (\d+) bytes", dll)
                if m and int(m.group(1)) > 4:
                    print("  [PASS] 屏幕重放字节数 = {}（>4，非空壳）".format(m.group(1)))
                else:
                    print("  [FAIL] 屏幕重放字节数异常: {}".format(
                        m.group(1) if m else "无记录"))
                    failures += 1

                # 自证复现条件：源尺寸必须 != 目标尺寸，否则"跳过重放"那条路根本不会触发
                mm = re.search(r"align begin win=(\d+)x(\d+) buf=(\d+)x(\d+) "
                               r"target=(\d+)x(\d+)", dll)
                if mm and (mm.group(1), mm.group(2)) != (mm.group(5), mm.group(6)):
                    print("  [PASS] 复现条件成立：源 {}x{} != 目标 {}x{}".format(
                        mm.group(1), mm.group(2), mm.group(5), mm.group(6)))
                else:
                    print("  [FAIL] 未构成尺寸不匹配（源与目标同尺寸）——用例前提不成立: {}".format(
                        mm.groups() if mm else "无 align 记录"))
                    failures += 1

            # 5. 光标位置必须与源终端一致
            time.sleep(1.0)
            dst_cursor = s.cursor("dst")
            if (dst_cursor[0], dst_cursor[1]) == (src_cursor[0], src_cursor[1]):
                print("  [PASS] 目标终端光标与源终端一致 = ({}, {})".format(
                    dst_cursor[0], dst_cursor[1]))
            else:
                print("  [FAIL] 光标位置不符：源 ({}, {}) vs 目标 ({}, {})".format(
                    src_cursor[0], src_cursor[1], dst_cursor[0], dst_cursor[1]))
                failures += 1
    except (RuntimeError, KeyError) as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
