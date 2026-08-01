"""特性: ENABLE_LINE_INPUT 开关（line/raw 模式）    类别: modes

链路: SendInput(type_text/type_enter) → WT → mediator → DLL ReadConsoleW_Detour

预期:
  - line 模式（LINE_INPUT 设置）：ReadConsoleW 回车才返回，返回行含尾部 \\r\\n
  - raw 模式（清除 LINE_INPUT）：ReadConsoleW 按键即返回，返回单字符

验证方式: 目标 ReadConsoleW 自检（含"未回车不返回"负等待）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "line_input_mode"

TARGET_BODY = '''
rec("READY", "PASS")
h_in = get_std_in()
# line 模式
set_mode(h_in, ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT)
_k.FlushConsoleInputBuffer(h_in)
buf = ctypes.create_unicode_buffer(64)
n = wintypes.DWORD(0)
ok = _k.ReadConsoleW(h_in, buf, 63, ctypes.byref(n), None)
if ok:
    rec("LINE_ON_RET", str(n.value) + " " + buf.value.encode("utf-8").hex())
else:
    rec("LINE_ON_RET", "FAIL")
# raw 模式
set_mode(h_in, ENABLE_PROCESSED_INPUT)
_k.FlushConsoleInputBuffer(h_in)
buf2 = ctypes.create_unicode_buffer(64)
n2 = wintypes.DWORD(0)
ok2 = _k.ReadConsoleW(h_in, buf2, 63, ctypes.byref(n2), None)
if ok2:
    rec("RAW_RET", str(n2.value) + " " + buf2.value.encode("utf-8").hex())
else:
    rec("RAW_RET", "FAIL")
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            # line 模式：输入 "x" 不回车 → 2s 内不应返回
            s.type_text("x")
            v_early = s.wait_result(NAME, "LINE_ON_RET", timeout=2.5)
            if v_early:
                print("  [FAIL] LINE_ON: 未回车即返回（line 模式失效）: {}".format(v_early))
                failures += 1
            else:
                print("  [PASS] LINE_ON 未回车不返回（line 模式生效）")

            # 回车 → 返回整行 "x\r\n"（n=3）
            s.type_enter()
            v = s.wait_result(NAME, "LINE_ON_RET", timeout=15.0)
            if not v:
                print("  [FAIL] LINE_ON_RET: 回车后无结果")
                failures += 1
            else:
                parts = v.split()
                if len(parts) == 2 and parts[0] == "3" and parts[1] == "780d0a":
                    print("  [PASS] LINE_ON 回车返回整行 (n=3 x\\r\\n)")
                else:
                    print("  [FAIL] LINE_ON_RET: {}（期望 3 780d0a）".format(v))
                    failures += 1

            # raw 模式：输入 "y" → 按键即返回单字符（n=1）
            s.type_text("y")
            v2 = s.wait_result(NAME, "RAW_RET", timeout=15.0)
            if not v2:
                print("  [FAIL] RAW_RET: 无结果（raw 模式未返回？）")
                failures += 1
            else:
                parts = v2.split()
                if len(parts) == 2 and parts[0] == "1" and parts[1] == "79":
                    print("  [PASS] RAW 按键即返回单字符 (n=1 y)")
                else:
                    print("  [FAIL] RAW_RET: {}（期望 1 79）".format(v2))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
