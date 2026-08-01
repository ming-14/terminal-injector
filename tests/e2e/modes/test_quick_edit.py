"""特性: ENABLE_QUICK_EDIT_MODE    类别: modes

链路: 目标 SetConsoleMode/GetConsoleMode（DLL ModeHooks）+ 输入路径

预期:
  - SetConsoleMode(QUICK_EDIT) 成功，GetConsoleMode 返回一致
  - 清除后 GetConsoleMode 返回一致
  - 设置后输入路径不被破坏（ReadConsoleW 正常返回内容）

验证方式: 目标自检 + 驱动输入验证
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "quick_edit"

TARGET_BODY = '''
rec("READY", "PASS")
h_in = get_std_in()
# 设置 QUICK_EDIT（附带常用输入标志）
ok1 = set_mode(h_in, ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_QUICK_EDIT_MODE)
g1 = get_mode(h_in)
rec("SET_QUICK", str(int(ok1)) + " " + hex(g1))
# 清除
ok2 = set_mode(h_in, ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)
g2 = get_mode(h_in)
rec("CLEAR_QUICK", str(int(ok2)) + " " + hex(g2))
# 设置后输入路径正常
ok3 = set_mode(h_in, ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT | ENABLE_QUICK_EDIT_MODE)
_k.FlushConsoleInputBuffer(h_in)
buf = ctypes.create_unicode_buffer(64)
n = wintypes.DWORD(0)
ok4 = _k.ReadConsoleW(h_in, buf, 63, ctypes.byref(n), None)
if ok4:
    rec("READ", str(n.value) + " " + buf.value.encode("utf-8").hex())
else:
    rec("READ", "FAIL")
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            v = s.wait_result(NAME, "SET_QUICK", timeout=10.0)
            if not v:
                print("  [FAIL] SET_QUICK: 无结果")
                failures += 1
            else:
                parts = v.split()
                if len(parts) == 2 and parts[0] == "1" and parts[1] == "0x47":
                    print("  [PASS] SET_QUICK 设置成功且 Get 一致 (0x47)")
                else:
                    print("  [FAIL] SET_QUICK: {}（期望 1 0x47）".format(v))
                    failures += 1

            v2 = s.wait_result(NAME, "CLEAR_QUICK", timeout=10.0)
            if not v2:
                print("  [FAIL] CLEAR_QUICK: 无结果")
                failures += 1
            else:
                parts = v2.split()
                if len(parts) == 2 and parts[0] == "1" and parts[1] == "0x7":
                    print("  [PASS] CLEAR_QUICK 清除后 Get 一致 (0x7)")
                else:
                    print("  [FAIL] CLEAR_QUICK: {}（期望 1 0x7）".format(v2))
                    failures += 1

            # 输入 "k" + Enter 验证输入路径
            s.type_text("k")
            s.type_enter()
            v3 = s.wait_result(NAME, "READ", timeout=15.0)
            if not v3:
                print("  [FAIL] READ: 无结果（QUICK_EDIT 破坏输入？）")
                failures += 1
            else:
                parts = v3.split()
                if len(parts) == 2 and parts[0] == "3" and parts[1] == "6b0d0a":
                    print("  [PASS] READ QUICK_EDIT 下输入正常 (n=3 k\\r\\n)")
                else:
                    print("  [FAIL] READ: {}（期望 3 6b0d0a）".format(v3))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
