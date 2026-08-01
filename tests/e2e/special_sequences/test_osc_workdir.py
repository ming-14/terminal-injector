"""特性: OSC 7 工作目录    类别: special_sequences

链路: 目标 WriteFile `\\x1b]7;file:///<cwd>\\x07` → DLL → mediator ChildVtOutput →
      ConPTY → WT（WT 用 OSC 7 更新标签页的工作目录）

预期:
  - mediator 日志含 OSC 7 序列字节
  - WT 新标签页继承该目录（可选校验，探测 WT 是否支持）

验证方式: 目标发送 + 驱动解析日志字节
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "osc_workdir"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
# OSC 7 工作目录（file:// URI）
ok, _ = write_bytes(h_out, b"\\x1b]7;file:///C:/Users/rikka/Desktop/e2e\\x07")
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
                pattern = (r"hex\[\d+\]=1B 5D 37 3B 66 69 6C 65 3A 2F 2F 2F "
                           r"43 3A 2F 55 73 65 72 73 2F 72 69 6B 6B 61 2F "
                           r"44 65 73 6B 74 6F 70 2F 74 65 73 74 73 5F 61 6C 6C 07")
                m = s.log().wait_for_regex(pattern, timeout=8.0)
                if m:
                    print("  [PASS] 日志含 OSC 7 工作目录序列（发送侧直通）")
                else:
                    print("  [FAIL] 日志未找到 OSC 7 工作目录序列")
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
