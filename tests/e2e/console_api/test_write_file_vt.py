"""特性: WriteFile(stdout) VT 模式直通    类别: console_api

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile(stdout, 文本) → DLL 直通 → mediator → WT

预期:
  - WriteFile 返回 TRUE 且写入数正确
  - 文本字节原样到达 mediator 日志（直通无翻译）
  - 结果文件 WRITE_FILE_RET=PASS

验证方式: 目标程序自检 + mediator 日志字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "write_file_vt"

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()
ok_mode = set_mode(h_out, ENABLE_VIRTUAL_TERMINAL_PROCESSING)
check("SET_VT_MODE", bool(ok_mode), "err={}".format(ctypes.get_last_error()))
ok, n = write_bytes(h_out, b"vt-hello")
check("WRITE_FILE_RET", bool(ok) and n == 8, "ok={} n={}".format(ok, n))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("SET_VT_MODE", "WRITE_FILE_RET"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            if not s.log().wait_for("76 74 2D 68 65 6C 6C 6F", timeout=10.0):
                print("  [FAIL] LOG_VT_HELLO: 直通字节未出现在日志")
                failures += 1
                s.log_tail()
            else:
                print("  [PASS] LOG_VT_HELLO (直通字节命中)")
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
