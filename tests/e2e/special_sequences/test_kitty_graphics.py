"""特性: Kitty 图形协议（iTerm 图像协议头）发送直通    类别: special_sequences

链路: 目标 WriteFile `\\x1b_G...\\x07` → DLL → mediator → WT

预期:
  - mediator 日志含 `1B 5F 47 66 3D 31 2C 61 3D 32 2C 6D 3D 33` 字节
    （`\\x1b_Gf=1,a=2,m=3` 头部）

验证方式: 目标发送 + 驱动解析日志字节（子串搜索）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "kitty_graphics"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
payload = b"\\x1b_Gf=1,a=2,m=3;AAAA\\x07"
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
                m = s.log().wait_for_regex(
                    r"1B 5F 47 66 3D 31 2C 61 3D 32 2C 6D 3D 33", timeout=8.0)
                if m:
                    print("  [PASS] 日志含 Kitty 图形头 1B 5F 47 66 3D 31 2C 61 3D 32 2C 6D 3D 33")
                else:
                    print("  [FAIL] 日志缺失 Kitty 图形头")
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
