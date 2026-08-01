"""特性: ScrollConsoleScreenBuffer 滚动矩形    类别: console_api

链路: 目标程序 ScrollConsoleScreenBufferW → DLL 翻译 → mediator → WT

预期:
  - 滚动 5x1 区域下移 1 行，返回 TRUE
  - 光标位置不变
  - 结果文件 SCROLL_RET=PASS

验证方式: 目标程序自检
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "scroll_screen_buffer"

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

src = SMALL_RECT(0, row, 4, row)          # 5x1 源区域
dst = COORD(0, row + 1)                    # 下移 1 行
fill = CHAR_INFO()
fill.Char = " "
fill.Attributes = 0
ok = _k.ScrollConsoleScreenBufferW(h_out, ctypes.byref(src), None, dst,
                                   ctypes.byref(fill))
check("SCROLL_RET", bool(ok), "err={}".format(ctypes.get_last_error()))
check("SCROLL_CURSOR_UNMOVED", cursor_pos(h_out) == (0, row),
      "expected (0,{}) got {}".format(row, cursor_pos(h_out)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("SCROLL_RET", "SCROLL_CURSOR_UNMOVED"):
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
