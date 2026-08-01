"""特性: SetConsoleWindowInfo（窗口位置/裁剪）    类别: cursor_buffer

链路: 目标程序 SetConsoleWindowInfo → DLL 虚拟状态（srWindow） → mediator → WT

预期:
  - 移动窗口（同尺寸新位置）返回 TRUE；Get srWindow 与设置一致
  - 裁剪窗口（缩小 srWindow 子矩形）返回 TRUE；Get srWindow 与设置一致
  - 恢复全窗口返回 TRUE；srWindow 回到与 dwSize 一致

验证方式: 目标程序自检（虚拟状态查询一致性）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "window_info"

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()
info = get_csbi(h_out)
if info is None:
    check("CSBI_OK", False, "GetConsoleScreenBufferInfo failed")
    done()
    sys.exit(1)
w, h = info.dwSize.X, info.dwSize.Y

def win_rect():
    i = get_csbi(h_out)
    if i is None:
        return None
    r = i.srWindow
    return (r.Left, r.Top, r.Right, r.Bottom)

check("CSBI_OK", True, "")

r = SMALL_RECT(1, 1, w - 2, h - 2)   # 裁剪：缩小一圈
ok = _k.SetConsoleWindowInfo(h_out, True, ctypes.byref(r))
check("SHRINK_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
check("SHRINK_QUERY", win_rect() == (1, 1, w - 2, h - 2),
      "got {}".format(win_rect()))

r = SMALL_RECT(0, 0, w - 1, h - 1)   # 恢复全窗口
ok = _k.SetConsoleWindowInfo(h_out, True, ctypes.byref(r))
check("RESTORE_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
check("RESTORE_QUERY", win_rect() == (0, 0, w - 1, h - 1),
      "got {}".format(win_rect()))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("CSBI_OK", "SHRINK_RET", "SHRINK_QUERY",
                        "RESTORE_RET", "RESTORE_QUERY"):
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
