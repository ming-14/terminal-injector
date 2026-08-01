"""特性: WriteConsoleW 写入 Unicode（中文/emoji）    类别: console_api

链路: 目标程序 WriteConsoleW → DLL ConsoleToVt 翻译（wcwidth 双宽）→ mediator → WT

预期:
  - 写 "测试"（2 字符）写入数=2，光标推进 4 列（2 列/字）
  - 写 "😀"（1 码点 = 2 wchar）写入数=2，光标推进 2 列
  - mediator 日志含 UTF-16 翻译字节

验证方式: 目标程序自检（GetConsoleScreenBufferInfo）+ mediator 日志字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "write_console_unicode"

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
ok, n = write_str(h_out, "\\u6d4b\\u8bd5")   # "测试"
check("WRITE_CJK_RET", bool(ok) and n == 2, "ok={} n={}".format(ok, n))
check("CURSOR_CJK", cursor_pos(h_out) == (4, row),
      "expected (4,{}) got {}".format(row, cursor_pos(h_out)))

row += 1
_k.SetConsoleCursorPosition(h_out, COORD(0, row))
ok, n = write_str(h_out, "\\U0001F600")      # "😀" 代理对
check("WRITE_EMOJI_RET", bool(ok) and n == 2, "ok={} n={}".format(ok, n))
check("CURSOR_EMOJI", cursor_pos(h_out) == (2, row),
      "expected (2,{}) got {}".format(row, cursor_pos(h_out)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("WRITE_CJK_RET", "CURSOR_CJK", "WRITE_EMOJI_RET", "CURSOR_EMOJI"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
