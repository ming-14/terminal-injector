"""特性: SetConsoleTextAttribute（属性 → SGR 翻译）    类别: console_api

链路: 目标程序 SetConsoleTextAttribute → DLL 翻译为 SGR → mediator → WT

预期:
  - SetConsoleTextAttribute 返回 TRUE
  - 设置红色后写 "X" 光标推进 1 列
  - SGR 颜色字节：0xC（RED|INTENSITY）应译为 \x1b[1;31;40m
    （BUG-001 已修复：Windows 位序 bit0=蓝 bit1=绿 bit2=红 → VT 索引重映射，
    修复前 0x4 被译为 34 蓝）

验证方式: 目标程序自检 + mediator 日志字节
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "set_text_attribute"

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()
info = get_csbi(h_out)
if info is None:
    check("CSBI", False, "GetConsoleScreenBufferInfo failed")
    done()
    sys.exit(1)
row = info.dwCursorPosition.Y + 1
_k.SetConsoleCursorPosition(h_out, COORD(0, row))

ok = _k.SetConsoleTextAttribute(h_out, FOREGROUND_RED | FOREGROUND_INTENSITY)
check("STA_RET", bool(ok), "err={}".format(ctypes.get_last_error()))

ok, n = write_str(h_out, "X")
check("WRITE_X_RET", bool(ok) and n == 1, "ok={} n={}".format(ok, n))
check("CURSOR_X", cursor_pos(h_out) == (1, row),
      "expected (1,{}) got {}".format(row, cursor_pos(h_out)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("STA_RET", "WRITE_X_RET", "CURSOR_X"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            # BUG-001 修复断言：0xC（RED|INTENSITY）应译为 \x1b[1;31;40m
            # （修复前 bit2=红 被当作 VT 索引 4 → 34 蓝）
            content = s.log().read_all()
            # 'X' 可能单独成批 flush(hex[1]=58 行尾),也可能与 SGR 合并(…6D 58 1B…)
            if not re.search(r"[ =]58(?=\s|$)", content):
                print("  [FAIL] LOG_WRITE_X: 日志未出现 X 字符字节")
                failures += 1
                s.log_tail()
            elif "1B 5B 31 3B 33 31 3B 34 30 6D" in content:
                print("  [PASS] LOG_SGR_RED: 0xC 译为 1;31;40m（红色正确）")
            else:
                print("  [FAIL] LOG_SGR_RED: 未观测到 1B 5B 31 3B 33 31 3B 34 30 6D")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
