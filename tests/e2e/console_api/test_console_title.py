"""特性: SetConsoleTitle / GetConsoleTitle（含 OSC 0 序列）    类别: console_api

链路: 目标程序 SetConsoleTitleW → DLL 缓存 + 翻译为 OSC 0 → mediator → WT

预期:
  - SetConsoleTitleW 返回 TRUE
  - GetConsoleTitleW 返回相同标题（DLL 状态缓存，Phase 14）
  - mediator 日志出现 OSC 0 序列（ESC ] 0 ; 标题）

验证方式: 目标程序自检 + mediator 日志字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "console_title"
TITLE = "ti-test-title"

TARGET_BODY = '''
rec("READY", "PASS")
ok = _k.SetConsoleTitleW("{}")
check("SET_TITLE_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
buf = ctypes.create_unicode_buffer(256)
n = _k.GetConsoleTitleW(buf, 256)
check("GET_TITLE", n > 0 and buf.value == "{}",
      "n=" + str(n) + " title=" + repr(buf.value))
done()
'''.format(TITLE, TITLE)


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("SET_TITLE_RET", "GET_TITLE"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            # OSC 0: ESC ] 0 ; title   → 1B 5D 30 3B 74 69 2D ...
            title_hex = "1B 5D 30 3B " + " ".join(
                "{:02X}".format(b) for b in TITLE.encode("ascii"))
            if s.log().wait_for(title_hex, timeout=10.0):
                print("  [PASS] LOG_OSC0 (OSC 0 序列字节命中)")
            else:
                print("  [FAIL] LOG_OSC0: 日志未出现 {}".format(title_hex))
                failures += 1
                s.log_tail()
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
