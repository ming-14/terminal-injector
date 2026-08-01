"""特性: DECGRA Sixel 图形发送直通    类别: special_sequences

链路: 目标 WriteFile `\\x1bP0;0;1q<图像数据>\\x1b\\\\` → DLL → mediator → WT

预期:
  - mediator 日志含 `1B 50 30 3B 30 3B 31 71`（DCS 头）与 `1B 5C`（ST）字节

验证方式: 目标发送 + 驱动解析日志字节（子串搜索）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "sixel"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
# DCS 0;0;1q + 2 像素数据（#1;2;1:0:0,~）+ ST
payload = b"\\x1bP0;0;1q#1;2;1:0:0,~\\x1b\\\\"
ok, _ = write_bytes(h_out, payload)
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
                # 报文可能跨多条 hex 行（BatchSender 分段），子串搜索
                m_h = s.log().wait_for_regex(
                    r"1B 50 30 3B 30 3B 31 71", timeout=8.0)
                m_t = s.log().wait_for_regex(r"1B 5C", timeout=8.0)
                if m_h and m_t:
                    print("  [PASS] 日志含 DCS 头 1B 50 30 3B 30 3B 31 71 与 ST")
                else:
                    print("  [FAIL] 日志缺失: dcs={} st={}".format(
                        bool(m_h), bool(m_t)))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
