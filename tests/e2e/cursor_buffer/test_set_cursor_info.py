"""特性: SetConsoleCursorInfo / GetConsoleCursorInfo（显隐与大小）    类别: cursor_buffer

链路: 目标程序 SetConsoleCursorInfo → DLL 虚拟状态 + 翻译 ?25h/l 序列 → mediator → WT

预期:
  - SetConsoleCursorInfo 返回 TRUE；Get 查询与设置一致（bVisible、dwSize）
  - 隐藏后日志出现 ?25l（1B 5B 3F 32 35 6C），显示出现 ?25h（1B 5B 3F 32 35 68）
  - dwSize（1~100 百分比）设置后查询一致（翻译为光标样式序列，不断言具体样式）

验证方式: 目标程序自检 + mediator 日志字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "set_cursor_info"

HIDE_HEX = "1B 5B 3F 32 35 6C"   # ?25l 隐藏光标
SHOW_HEX = "1B 5B 3F 32 35 68"   # ?25h 显示光标

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()

def get_ci():
    ci = CONSOLE_CURSOR_INFO()
    if not _k.GetConsoleCursorInfo(h_out, ctypes.byref(ci)):
        return None
    return ci

ci = get_ci()
check("GET_CI_OK", ci is not None, "GetConsoleCursorInfo failed")
if ci is not None:
    check("DEFAULT_VISIBLE", bool(ci.bVisible), "default bVisible={}".format(ci.bVisible))

ci = CONSOLE_CURSOR_INFO(20, 0)
ok = _k.SetConsoleCursorInfo(h_out, ctypes.byref(ci))
check("HIDE_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
ci2 = get_ci()
check("HIDE_QUERY", ci2 is not None and not ci2.bVisible,
      "bVisible={}".format(ci2.bVisible if ci2 else "?"))

ci = CONSOLE_CURSOR_INFO(50, 1)
ok = _k.SetConsoleCursorInfo(h_out, ctypes.byref(ci))
check("SHOW_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
ci2 = get_ci()
check("SHOW_QUERY", ci2 is not None and bool(ci2.bVisible) and ci2.dwSize == 50,
      "bVisible={} dwSize={}".format(ci2.bVisible if ci2 else "?", ci2.dwSize if ci2 else "?"))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("GET_CI_OK", "DEFAULT_VISIBLE", "HIDE_RET",
                        "HIDE_QUERY", "SHOW_RET", "SHOW_QUERY"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            content = s.log().read_all()
            if HIDE_HEX in content:
                print("  [PASS] LOG_CURSOR_HIDE (?25l 字节命中)")
            else:
                print("  [FAIL] LOG_CURSOR_HIDE: 日志未出现 ?25l")
                failures += 1
            if SHOW_HEX in content:
                print("  [PASS] LOG_CURSOR_SHOW (?25h 字节命中)")
            else:
                print("  [FAIL] LOG_CURSOR_SHOW: 日志未出现 ?25h")
                failures += 1
            if failures > 0:
                s.log_tail()
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
