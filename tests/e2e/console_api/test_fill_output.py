"""特性: FillConsoleOutputCharacter / FillConsoleOutputAttribute    类别: console_api

链路: 目标程序 FillConsoleOutputCharacterW / FillConsoleOutputAttribute
      → DLL 翻译 → mediator → WT

预期:
  - 填充 10 个 '#' 返回 TRUE 且填充数=10
  - 属性填充返回 TRUE 且填充数=10
  - 光标位置不变（填充不移动光标）
  - mediator 日志出现 '#' 翻译字节（diff 优化：1 个 '#' + CUF(n-1)，不断言连续）

验证方式: 目标程序自检 + mediator 日志字节
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "fill_output"

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

n = wintypes.DWORD(0)
ok = _k.FillConsoleOutputCharacterW(h_out, "#", 10, COORD(0, row), ctypes.byref(n))
check("FILL_CHAR_RET", bool(ok) and n.value == 10, "ok={} n={}".format(ok, n.value))

n = wintypes.DWORD(0)
ok = _k.FillConsoleOutputAttribute(h_out, BACKGROUND_GREEN, 10, COORD(0, row),
                                   ctypes.byref(n))
check("FILL_ATTR_RET", bool(ok) and n.value == 10, "ok={} n={}".format(ok, n.value))

check("FILL_CURSOR_UNMOVED", cursor_pos(h_out) == (0, row),
      "expected (0,{}) got {}".format(row, cursor_pos(h_out)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("FILL_CHAR_RET", "FILL_ATTR_RET", "FILL_CURSOR_UNMOVED"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            content = s.log().read_all()
            # '#' 可能单独成批 flush(hex[1]=23 行尾),也可能与其他字节合并
            if re.search(r"[ =]23(?=\s|$)", content):
                print("  [PASS] LOG_FILL_CHARS (填充字符字节命中)")
            else:
                print("  [FAIL] LOG_FILL_CHARS: 日志未出现 23 字节")
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
