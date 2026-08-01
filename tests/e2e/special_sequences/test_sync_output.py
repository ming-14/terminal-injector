"""特性: 同步输出模式（?2026h/l）    类别: special_sequences

链路: 目标 WriteFile `\\x1b[?2026h` / `\\x1b[?2026l` → DLL → mediator →
      ConPTY → WT

预期:
  - mediator 日志含 `1B 5B 3F 32 30 32 36 68`（h）与 `6C`（l）字节

验证方式: 目标发送 + 驱动解析日志字节
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "sync_output"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
ok1, _ = write_bytes(h_out, b"\\x1b[?2026h")
ok2, _ = write_bytes(h_out, b"\\x1b[?2026l")
rec("SENT", "%d,%d" % (int(ok1), int(ok2)))
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
                # 注意: 两次写入间隔短，BatchSender 会合并为一条 ChildVtOutput，
                # 不能用 "hex[n]=前缀" 断言两条独立行，改为无前缀子串搜索。
                m_h = s.log().wait_for_regex(
                    r"1B 5B 3F 32 30 32 36 68", timeout=8.0)
                m_l = s.log().wait_for_regex(
                    r"1B 5B 3F 32 30 32 36 6C", timeout=8.0)
                if m_h and m_l:
                    print("  [PASS] 日志含 ?2026h 与 ?2026l（发送侧直通）")
                else:
                    print("  [FAIL] 日志缺失: h={} l={}".format(
                        bool(m_h), bool(m_l)))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
