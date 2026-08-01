"""特性: WriteConsoleW 写入 ASCII    类别: console_api

链路: 目标程序 WriteConsoleW → DLL ConsoleToVt 翻译 → mediator → WT

预期:
  - 写 "Hello"（5 字符）返回 TRUE 且写入数=5
  - 虚拟状态光标从 (0,row) 精确推进到 (5,row)
  - mediator 日志含 "Hello" 的 UTF-16 翻译字节（48 65 6C 6C 6F）

验证方式: 目标程序自检（GetConsoleScreenBufferInfo）+ mediator 日志字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "write_console_ascii"

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
ok, n = write_str(h_out, "Hello")
check("WRITE_RET", bool(ok) and n == 5, "ok={} n={}".format(ok, n))
check("CURSOR_5", cursor_pos(h_out) == (5, row),
      "expected (5,{}) got {}".format(row, cursor_pos(h_out)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("WRITE_RET", "CURSOR_5"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            if not s.log().wait_for("48 65 6C 6C 6F", timeout=10.0):
                print("  [FAIL] LOG_HELLO: 日志未出现 Hello 翻译字节")
                failures += 1
                s.log_tail()
            else:
                print("  [PASS] LOG_HELLO (翻译字节命中)")
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
