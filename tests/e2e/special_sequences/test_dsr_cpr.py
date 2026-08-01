"""特性: DSR CPR 光标位置查询（CSI 6n）    类别: special_sequences

链路: 目标 WriteFile `\\x1b[6n` → mediator → ConPTY → WT 响应
      `\\x1b[<row>;<col>R` → mediator route 回 child → 目标 os.read 读原始字节

预期（Phase 15 已实现，必须 PASS）:
  - 目标 os.read 收到 `1B 5B <row> 3B <col> 52`（R=0x52）
  - row/col 为 1-based 正整数

验证方式: 目标 VT_INPUT + os.read 读响应并 hex 上报
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "dsr_cpr"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_VIRTUAL_TERMINAL_INPUT)
h_out = get_std_out()
ok, _ = write_bytes(h_out, b"\\x1b[6n")
rec("SENT", str(int(ok)))
import os as _os
b = _os.read(0, 64)
rec("GOT", b.hex())
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v = s.wait_result(NAME, "SENT", timeout=15.0)
            if not v:
                print("  [FAIL] SENT: 无结果")
                failures += 1
            else:
                v_got = s.wait_result(NAME, "GOT", timeout=15.0)
                if not v_got:
                    print("  [FAIL] GOT: 无结果（CPR 响应未到达）")
                    failures += 1
                else:
                    hexs = v_got.lower()
                    print("  [INFO] 响应 hex: {}".format(hexs))
                    if hexs.startswith("1b5b") and hexs.endswith("52") \
                            and "3b" in hexs:
                        print("  [PASS] 收到 CPR 响应 1B 5B <row> 3B <col> 52")
                    else:
                        print("  [FAIL] CPR 响应格式异常（期望 1B 5B r 3B c 52）")
                        failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
