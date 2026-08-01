"""特性: OSC 8 超链接    类别: special_sequences

链路: 目标 WriteFile `\\x1b]8;;<uri>\\x1b\\` → DLL → mediator ChildVtOutput →
      ConPTY → WT

预期:
  - mediator 日志含 OSC 8 序列字节（`1B 5D 38 3B 3B <uri> 1B 5C`）
  - WT 支持超链接渲染（可选校验：不破坏后续输出）

验证方式: 目标发送 + 驱动解析日志字节
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "osc_hyperlink"

URI = "https://example.com/e2e"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
ok, _ = write_bytes(h_out, b"\\x1b]8;;''' + URI + r'''\x1b\\")
rec("SENT", str(int(ok)))
time.sleep(1.0)
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
                # hex: 1B 5D 38 3B 3B <uri> 1B 5C
                uri_hex = " ".join("{:02X}".format(b) for b in URI.encode())
                pattern = r"hex\[\d+\]=1B 5D 38 3B 3B " + uri_hex + " 1B 5C"
                m = s.log().wait_for_regex(pattern, timeout=8.0)
                if m:
                    print("  [PASS] 日志含 OSC 8 超链接序列（发送侧直通）")
                else:
                    print("  [FAIL] 日志未找到 OSC 8 超链接序列")
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
