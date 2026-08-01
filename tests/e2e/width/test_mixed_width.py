"""特性: 混合宽度字符光标推进    类别: width

链路: 目标 WriteConsoleW("A中😀B") → DLL wcwidth 逐字符（1+2+2+1） →
      DLL 虚拟光标 X 推进 6 → 退出时 ChildExitSync 上报 cursor

预期（wcwidth 集成，必须 PASS）:
  - ChildExitSync sent cursor=(基线.X + 6, <Y>)
  - 基线 = python LazyInit aligned 光标（HelloAck 的 WT 真实位置，
    2026-08-02 修复；修复前基线为 ConHost 陈旧快照 (0,4)，
    断言曾直接假设 X=0）

验证方式: 目标 WriteConsoleW + 驱动解析 ChildExitSync 日志
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from common import childlog

NAME = "mixed_width"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes
k32 = ctypes.windll.kernel32
h_out = get_std_out()
text = "A\u4e2d\U0001F600B"  # A中😀B
wbuf = ctypes.create_unicode_buffer(text)
written = ctypes.c_ulong(0)
ok = k32.WriteConsoleW(h_out, wbuf, len(wbuf.value.encode("utf-16-le")) // 2,
                       ctypes.byref(written), None)
rec("RESULT", "{} {}".format(int(ok), written.value))
time.sleep(1.5)
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v = s.wait_result(NAME, "RESULT", timeout=15.0)
            if not v:
                print("  [FAIL] RESULT: 无结果")
                failures += 1
            else:
                ok, written = (int(x) for x in v.split()[:2])
                if ok and written == 5:
                    print("  [PASS] WriteConsoleW 写入 5 wchar")
                else:
                    print("  [FAIL] WriteConsoleW: ok={} written={}（期望 1/5 wchar）".format(
                        ok, written))
                    failures += 1
                baseline = childlog.find_child_aligned_baseline()
                cur = childlog.wait_child_exit_cursor(s.log())
                if not cur:
                    print("  [FAIL] 未收到 ChildExitSync cursor")
                    failures += 1
                elif baseline is None:
                    print("  [FAIL] 未解析到 aligned 基线（X 断言无法计算）")
                    failures += 1
                else:
                    x, y = cur
                    bx, by = baseline
                    exp_x = bx + 6
                    print("  [INFO] aligned=({},{}) ChildExitSync=({},{}) 期望 X={}".format(
                        bx, by, x, y, exp_x))
                    if x == exp_x:
                        print("  [PASS] \"A中\\U0001f600B\" 后 X={}（基线 {}+1+2+2+1 列）".format(
                            x, bx))
                    else:
                        print("  [FAIL] X={}（期望 {}）".format(x, exp_x))
                        failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
