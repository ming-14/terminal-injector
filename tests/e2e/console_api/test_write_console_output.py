"""特性: WriteConsoleOutput 字符+属性矩阵写入    类别: console_api

链路: 目标程序 WriteConsoleOutputW(2x1 矩阵) → DLL diff 增量 → mediator → WT

预期:
  - 返回 TRUE，区域被更新（返回值验证）
  - 光标位置不变（WriteConsoleOutput 不移动光标）
  - mediator 日志出现矩阵字符 "AB" 的翻译字节（41 42，diff 增量输出）

验证方式: 目标程序自检 + mediator 日志字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "write_console_output"

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

# 2x1 矩阵：A(红) B(红)
buf = (CHAR_INFO * 2)()
buf[0].Char = "A"
buf[0].Attributes = FOREGROUND_RED
buf[1].Char = "B"
buf[1].Attributes = FOREGROUND_RED
region = SMALL_RECT(0, row, 1, row)
ok = _k.WriteConsoleOutputW(h_out, buf, COORD(2, 1), COORD(0, 0),
                            ctypes.byref(region))
check("WCO_RET", bool(ok), "err={}".format(ctypes.get_last_error()))
check("WCO_CURSOR_UNMOVED", cursor_pos(h_out) == (0, row),
      "expected (0,{}) got {}".format(row, cursor_pos(h_out)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("WCO_RET", "WCO_CURSOR_UNMOVED"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            # diff 增量：矩阵字符 AB 出现在日志
            content = s.log().read_all()
            if "41 42" in content:
                print("  [PASS] LOG_MATRIX_AB (diff 增量字节命中)")
            else:
                print("  [FAIL] LOG_MATRIX_AB: 日志未出现 41 42")
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
