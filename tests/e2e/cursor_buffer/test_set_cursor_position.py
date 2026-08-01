"""特性: SetConsoleCursorPosition（POS 更新 + CUP 序列）    类别: cursor_buffer

链路: 目标程序 SetConsoleCursorPosition → DLL 虚拟状态更新 + 翻译 CUP → mediator → WT

预期:
  - 返回 TRUE；随后 GetConsoleScreenBufferInfo 光标 = 设置值（虚拟状态，Phase 14）
  - mediator 日志出现 CUP 序列（1-based: ESC [ row ; col H）
  - 在设置位置写 "X" 后，光标推进到 (x+1, y)（写入从设置位置开始）

验证方式: 目标程序自检 + mediator 日志字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "set_cursor_position"

CUP_HEX = "1B 5B 37 3B 31 31 48"   # ESC [ 7 ; 11 H（1-based: 行 7 列 11）

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()
ok = _k.SetConsoleCursorPosition(h_out, COORD(10, 6))
check("POS_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
check("POS_QUERY", cursor_pos(h_out) == (10, 6),
      "got {}".format(cursor_pos(h_out)))
ok, n = write_str(h_out, "X")
check("WRITE_X_RET", bool(ok) and n == 1, "ok={} n={}".format(ok, n))
check("POS_AFTER_WRITE", cursor_pos(h_out) == (11, 6),
      "got {}".format(cursor_pos(h_out)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("POS_RET", "POS_QUERY", "WRITE_X_RET", "POS_AFTER_WRITE"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            if s.log().wait_for(CUP_HEX, timeout=10.0):
                print("  [PASS] LOG_CUP (CUP 序列字节命中)")
            else:
                print("  [FAIL] LOG_CUP: 日志未出现 {}".format(CUP_HEX))
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
