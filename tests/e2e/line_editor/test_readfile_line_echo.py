"""特性: ReadFile(stdin) 行编辑回显（LINE+ECHO）    类别: line_editor

回归测试（2026-09-25 termtest 16 色段「问句后空行丢失」修复）:

现象
  注入进 WT 后跑 termtest，16 色段里「问句行」与「[SKIP]/结果行」之间
  本该有一个空行，劫持后消失；ConHost 原生运行则正常。用户要求「期望一致」。

根因（两处，均在 DLL 输入链）
  1. ReadFile(stdin) 在 ENABLE_LINE_INPUT+ENABLE_ECHO_INPUT 下**未做行编辑/
     按键回显**——ConHost 此时把 ReadFile(stdin) 当 ReadConsoleA 处理：逐键回显
     到屏幕、行缓冲到 Enter。缺回显 → 按 Enter 该出现的换行（空行）没有。
  2. 回显字节只 SendToMediator、未喂 VtCursorTracker → 追踪器显示光标停在
     回显前位置；随后子进程输出（OutputHooks.SyncChildVtCursorBeforeWrite）
     用追踪器补发 CursorPosition(ESC[row;1H)，落到回显同一行，把空行覆盖掉。

修复
  ReadFile_Detour 新增行编辑分支（复用 LineEditor，与 ReadConsoleW/A 同实现）：
    - 逐键回显经 EmitLineEcho 走 VtSgrFilter→VtCursorTracker→SendToMediator
      （与真实输出同一套处理，追踪器随回显推进）
    - 行结束按原因构造返回字节：Enter → lineOut+"\\r\\n"（**空行也补**）、
      Ctrl+C → ""（0 字节但返回 TRUE，非 EOF）、Ctrl+Z → 截断行

ConHost 实测基准（tests/_probe/t_readfile_gt2.py / t_echo_rules.py）:
    喂 "ab\\r"      → 回显 `ab` 再 `\\r\\n`；os.read 一次得 b"ab\\r\\n"
    只按 Enter（空行）→ b"\\r\\n"（2 字节，**空行也带换行**）
    Ctrl+C          → b""（0 字节，成功非 EOF）

验证方式: 目标脚本用 os.read(stdin) 读一行，把收到字节 hex 记录到结果文件；
          驱动侧断言「文本+Enter = 61 62 0d 0a」「空行+Enter = 0d 0a」。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "readfile_line_echo"

# 目标启用 LINE+ECHO（termtest 默认控制台模式），用 os.read 读 stdin。
# os.read 走 ReadFile(stdin)，命中新增的行编辑分支。
TARGET_BODY = '''
import os
rec("READY", "PASS")
h_in = get_std_in()
# termtest 默认模式：LINE_INPUT | ECHO_INPUT | PROCESSED_INPUT
set_mode(h_in, ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_PROCESSED_INPUT)
time.sleep(2.0)  # 等 DLL LazyInit/回显链路就绪

def read_line_hex():
    """读一行，返回收到的字节 hex（strip）。Ctrl+C 0 字节亦成功。"""
    try:
        data = os.read(0, 4096)
    except Exception as e:
        return "ERR:" + repr(e)
    return data.hex()

# 第 1 次：文本 "ab" + Enter → 期望 61 62 0d 0a
h1 = read_line_hex()
rec("LINE1", h1)

# 第 2 次：空行 + Enter → 期望 0d 0a（空行也补换行，不得 0 字节/EOF）
h2 = read_line_hex()
rec("LINE2", h2)

done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            # 第 1 次：输入 "ab" + Enter
            s.type_text("ab")
            time.sleep(0.2)
            s.type_enter()
            v1 = s.wait_result(NAME, "LINE1", timeout=15.0)
            if not v1:
                print("  [FAIL] LINE1: 无结果（ReadFile 未返回？行编辑分支未命中）")
                failures += 1
            elif v1 == "61620d0a":
                print("  [PASS] 文本+Enter → os.read 得 61 62 0d 0a (\"ab\\r\\n\")")
            else:
                print("  [FAIL] LINE1={}（期望 61620d0a = \"ab\\r\\n\"）".format(v1))
                failures += 1

            # 第 2 次：直接 Enter（空行）
            s.type_enter()
            v2 = s.wait_result(NAME, "LINE2", timeout=15.0)
            if not v2:
                print("  [FAIL] LINE2: 无结果（空行 Enter 返回 0 字节被当 EOF？）")
                failures += 1
            elif v2 == "0d0a":
                print("  [PASS] 空行+Enter → os.read 得 0d 0a（空行也补换行，非 EOF）")
            else:
                print("  [FAIL] LINE2={}（期望 0d0a；空行 Enter 必须返回 \\r\\n）".format(v2))
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
