"""特性: SGR 基础颜色（前景 30-37 / 背景 40-47）+ 256 色 + 真彩色    类别: vt_output

链路: 目标程序 SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)
      → WriteFile 直通 → DLL → mediator → WT

预期:
  - 目标脚本启用 VT 输出模式后，WriteFile 发出的 \x1b[31m / \x1b[42m / \x1b[38;5;196m /
    \x1b[38;2;255;0;0m 序列全部原样到达 mediator 日志（VtOutput/ChildVtOutput hex 匹配）
  - 注意：VT 直通模式下虚拟状态不跟踪光标（程序自维护，Phase 13 设计），
    故本测试不做光标断言；光标推进断言属于 console_api 类别（WriteConsoleW 翻译链）
  - 结果文件 DONE=1

验证方式: mediator 日志 VtOutput 字节 + 目标程序自检结果文件
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

# 目标脚本正文（共享前导 TARGET_PREAMBLE 已提供 ctypes 绑定与 check()/done()）
TARGET_BODY = '''
rec("READY", "PASS")

h_out = get_std_out()

# 启用 VT 输出直通（WriteFile 直通路径）
ok = set_mode(h_out, ENABLE_VIRTUAL_TERMINAL_PROCESSING)
check("SET_VT_MODE", bool(ok), "SetConsoleMode err={}".format(ctypes.get_last_error()))

# 光标归零到新行（验证 SetConsoleCursorPosition 虚拟状态仍可用）
info = get_csbi(h_out)
if info is None:
    check("CSBI", False, "GetConsoleScreenBufferInfo failed")
    done()
    sys.exit(1)
row = info.dwCursorPosition.Y + 1
_k.SetConsoleCursorPosition(h_out, COORD(0, row))
check("SET_POS", cursor_pos(h_out) == (0, row), "pos={}".format(cursor_pos(h_out)))

# 16 色前景/背景 + 256 色 + 真彩色，全部经 WriteFile 直通
write_bytes(h_out, b"\\x1b[31mRED\\x1b[0m")
write_bytes(h_out, b"\\x1b[42mBG\\x1b[0m")
write_bytes(h_out, b"\\x1b[38;5;196mX\\x1b[0m")
write_bytes(h_out, b"\\x1b[38;2;255;0;0mX\\x1b[0m")

done()
'''


def run() -> int:
    failures = 0
    name = "sgr_basic_colors"
    result_mod.clear_result(name)
    try:
        with TestSession() as s:
            s.run_target(name, TARGET_BODY, ready_key="READY", ready_timeout=30.0)

            # 目标脚本自检断言
            for key in ("SET_VT_MODE", "SET_POS"):
                v = s.wait_result(name, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1

            # 日志字节断言：所有 SGR 序列在 mediator VtOutput 日志中
            # 注意：多个序列可能在同一条 VtOutput 行，先等任一出现，再对全文匹配
            log = s.log()
            first = ("1B 5B 33 31 6D", "LOG_FG_31M")   # \\x1b[31m
            log_checks = [
                ("1B 5B 34 32 6D", "LOG_BG_42M"),        # \\x1b[42m
                ("1B 5B 33 38 3B 35 3B 31 39 36 6D", "LOG_256"),      # \x1b[38;5;196m
                ("1B 5B 33 38 3B 32 3B 32 35 35 3B 30 3B 30 6D", "LOG_TRUE"),  # \x1b[38;2;255;0;0m
            ]
            if log.wait_for(first[0], timeout=10.0):
                print("  [PASS] {} (VtOutput 字节命中)".format(first[1]))
                content = log.read_all()
                for pattern, key in log_checks:
                    if pattern in content:
                        print("  [PASS] {} (VtOutput 字节命中)".format(key))
                    else:
                        print("  [FAIL] {}: 日志未出现 {}".format(key, pattern))
                        failures += 1
            else:
                print("  [FAIL] {}: 日志未出现 {}".format(first[1], first[0]))
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
