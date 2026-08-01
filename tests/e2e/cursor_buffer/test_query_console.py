"""特性: GetConsoleWindow / GetLargestConsoleWindowSize / GetConsoleProcessList    类别: cursor_buffer

链路: 目标程序查询控制台信息 → DLL 返回合理值（不崩溃）

预期:
  - GetConsoleWindow 返回非零 HWND（真实终端窗口句柄）
  - GetLargestConsoleWindowSize 宽 >= 80 且高 >= 24（不小于当前窗口）
  - GetConsoleProcessList 返回 >= 1 个进程（cmd + python + 注入器）
  - GetConsoleMode(stdout) 含 ENABLE_VIRTUAL_TERMINAL_PROCESSING（强制 VT）

验证方式: 目标程序自检（合理性断言）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "query_console"

TARGET_BODY = '''
rec("READY", "PASS")
hwnd = _k.GetConsoleWindow()
check("WIN_NONZERO", bool(hwnd), "hwnd=0")
maxsz = _k.GetLargestConsoleWindowSize(get_std_out())
check("MAXW_REASONABLE", maxsz.X >= 80 and maxsz.Y >= 24,
      "max=({},{})".format(maxsz.X, maxsz.Y))
ids = (wintypes.DWORD * 16)()
n = _k.GetConsoleProcessList(ids, 16)
check("PROCLIST_GE1", n >= 1, "n={}".format(n))
check("PROCLIST_REASONABLE", 1 <= n <= 16, "n={}".format(n))
m = get_mode(get_std_out())
check("MODE_HAS_VT", (m & ENABLE_VIRTUAL_TERMINAL_PROCESSING) != 0,
      "mode={:#x}".format(m))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("WIN_NONZERO", "MAXW_REASONABLE",
                        "PROCLIST_GE1", "PROCLIST_REASONABLE", "MODE_HAS_VT"):
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
