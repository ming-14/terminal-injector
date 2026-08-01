"""特性: OSC 11;? 背景色查询    类别: special_sequences

链路: 目标 WriteFile `\\x1b]11;?\\x07` → WT → 响应 `\\x1b]11;rgb:rrrr/gggg/bbbb\\x07`
      → 目标 os.read

预期:
  - 收到 `1B 5D 31 31 3B 72 67 62 3A ... 07` → PASS
  - 4s 无响应 → UNSUPPORTED（WT 不支持该查询，不允许 FAIL）

验证方式: 目标 VT_INPUT + 线程超时读响应
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "osc_color_query"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_VIRTUAL_TERMINAL_INPUT)
h_out = get_std_out()
ok, _ = write_bytes(h_out, b"\\x1b]11;?\\x07")
rec("SENT", str(int(ok)))
import os as _os
import threading
res = []
t = threading.Thread(target=lambda: res.append(_os.read(0, 256)))
t.daemon = True
t.start()
t.join(4.0)
b = res[0] if res else b""
rec("GOT", b.hex() if b else "TIMEOUT")
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
                    print("  [FAIL] GOT: 无结果")
                    failures += 1
                elif v_got == "TIMEOUT":
                    print("  [UNSUPPORTED] 4s 无 OSC 11;? 响应（WT 不支持背景色查询）")
                else:
                    hexs = v_got.lower()
                    print("  [INFO] 响应 hex: {}".format(hexs))
                    if hexs.startswith("1b5d31313b") and \
                            (hexs.endswith("07") or hexs.endswith("1b5c")):
                        print("  [PASS] 收到 OSC 11 背景色响应 (rgb, 终止符 {})".format(
                            "BEL" if hexs.endswith("07") else "ST"))
                    else:
                        print("  [FAIL] OSC 11 响应格式异常: {}".format(hexs))
                        failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
