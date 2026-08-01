"""特性: GetConsoleScreenBufferInfo（窗口/缓冲尺寸、光标、属性）    类别: cursor_buffer

链路: 目标程序 GetConsoleScreenBufferInfo → DLL 虚拟状态（Phase 14，与 mediator 同步）

预期:
  - dwSize 与 WT 实际尺寸同步（宽 80~300，高 24~200 合理范围）
  - srWindow 与 dwSize 一致（窗口=缓冲，虚拟状态语义）
  - dwMaximumWindowSize >= srWindow 尺寸
  - 光标位置在缓冲范围内，且 = 真实终端渲染位置（虚拟状态）
  - wAttributes 非零（默认黑底白字）

验证方式: 目标程序自检（范围/一致性断言，不依赖具体终端尺寸）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "get_screen_buffer_info"

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()
info = get_csbi(h_out)
if info is None:
    check("CSBI_OK", False, "GetConsoleScreenBufferInfo failed")
    done()
    sys.exit(1)
check("CSBI_OK", True, "")
w, h = info.dwSize.X, info.dwSize.Y
check("SIZE_RANGE", 80 <= w <= 300 and 24 <= h <= 200,
      "dwSize=({},{})".format(w, h))
win_w = info.srWindow.Right - info.srWindow.Left + 1
win_h = info.srWindow.Bottom - info.srWindow.Top + 1
check("WINDOW_EQ_BUFFER", win_w == w and win_h == h,
      "srWindow=({}x{}) dwSize=({}x{})".format(win_w, win_h, w, h))
check("MAXWIN_GE_WINDOW",
      info.dwMaximumWindowSize.X >= win_w and info.dwMaximumWindowSize.Y >= win_h,
      "maxwin=({},{}) win=({},{})".format(
          info.dwMaximumWindowSize.X, info.dwMaximumWindowSize.Y, win_w, win_h))
cx, cy = info.dwCursorPosition.X, info.dwCursorPosition.Y
check("CURSOR_IN_RANGE", 0 <= cx < w and 0 <= cy < h,
      "cursor=({},{}) size=({},{})".format(cx, cy, w, h))
check("ATTR_NONZERO", info.wAttributes != 0, "attr={}".format(info.wAttributes))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("CSBI_OK", "SIZE_RANGE", "WINDOW_EQ_BUFFER",
                        "MAXWIN_GE_WINDOW", "CURSOR_IN_RANGE", "ATTR_NONZERO"):
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
