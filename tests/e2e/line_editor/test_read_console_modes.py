"""特性: ReadConsoleW line/raw 模式    类别: line_editor

链路: SendInput(type_text) → WT → mediator → DLL ReadConsoleW_Detour → 目标

预期:
  - line 模式（ENABLE_LINE_INPUT）：ReadConsoleW 回车才返回，返回完整行内容
  - raw 模式（清除 LINE_INPUT）：ReadConsoleW 按键即返回，返回单字符
  - 两种模式返回的 ok/n/内容正确

验证方式: 目标脚本 ReadConsoleW 自检记录到结果文件
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "read_console_modes"

TARGET_BODY = '''
rec("READY", "PASS")
h_in = get_std_in()
set_mode(h_in, ENABLE_LINE_INPUT | ENABLE_PROCESSED_INPUT)
_k.FlushConsoleInputBuffer(h_in)
buf = ctypes.create_unicode_buffer(64)
n = wintypes.DWORD(0)
ok = _k.ReadConsoleW(h_in, buf, 63, ctypes.byref(n), None)
rec("LINE_RET", str(int(ok)) + " " + str(n.value) + " " + (buf.value if ok else ""))
set_mode(h_in, 0)
_k.FlushConsoleInputBuffer(h_in)
buf2 = ctypes.create_unicode_buffer(64)
n2 = wintypes.DWORD(0)
ok2 = _k.ReadConsoleW(h_in, buf2, 63, ctypes.byref(n2), None)
rec("RAW_RET", str(int(ok2)) + " " + str(n2.value) + " " + (buf2.value if ok2 else ""))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            # line 模式：输入 "ab" + Enter → 返回行含尾部 \r\n（ReadConsoleW 语义）
            s.type_text("ab")
            s.type_enter()
            v = s.wait_result(NAME, "LINE_RET", timeout=15.0)
            if not v:
                print("  [FAIL] LINE_RET: 无结果（ReadConsoleW 未返回？）")
                failures += 1
            else:
                parts = v.split()
                if len(parts) == 3 and parts[0] == "1" and parts[1] == "4" and parts[2] == "ab":
                    print("  [PASS] LINE 模式回车返回整行含 \\r\\n (ok=1 n=4 ab)")
                else:
                    print("  [FAIL] LINE_RET: {}（期望 1 4 ab）".format(v))
                    failures += 1

            # raw 模式：输入 "x" → 按键即返回单字符
            s.type_text("x")
            v2 = s.wait_result(NAME, "RAW_RET", timeout=15.0)
            if not v2:
                print("  [FAIL] RAW_RET: 无结果（raw 模式未返回？）")
                failures += 1
            else:
                parts = v2.split()
                if len(parts) == 3 and parts[0] == "1" and parts[1] == "1" and parts[2] == "x":
                    print("  [PASS] RAW 模式按键即返回单字符 (ok=1 n=1 x)")
                else:
                    print("  [FAIL] RAW_RET: {}（期望 1 1 x）".format(v2))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
