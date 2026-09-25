"""特性: ENABLE_PROCESSED_OUTPUT（换行处理）    类别: modes

链路: 目标 SetConsoleMode/WriteFile → DLL ModeHooks/WriteFile_Detour（VT 直通）

预期（与 ConHost 实际渲染语义对齐）:
  - SetConsoleMode(输出句柄, PROCESSED_OUTPUT) → Get == 0x5（强制保留 VT_PROCESSING）
  - VT 直通模式下 WriteFile 写 "a\\nb"，线上字节为 61 0D 0A 62：
    裸 LF 在 Ground 态被 VtSgrFilter 补 CR（ConHost 把裸 LF 当 CR+LF）。

与原生 ConHost 的一致性（2026-09-25 修复后）:
  - 原生 ConHost：WriteFile 写 \\n 时按 CR+LF 解释 → 屏幕回列 0 换行
  - 工程：VtSgrFilter 在直通入口把裸 LF 归一为 CRLF，使 WT 侧渲染
    与 ConHost 一致（WT/wezterm 把裸 LF 当纯 LF，保持列号 → 会右移错位）
  - 修复前该测试断言 61 0A 62（裸 LF 原样），记录的是错位行为

验证方式: 目标自检 + mediator 日志 ChildVtOutput 精确字节
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "processed_output"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
ok1 = set_mode(h_out, ENABLE_PROCESSED_OUTPUT)
g1 = get_mode(h_out)
rec("SET_GET", str(int(ok1)) + " " + hex(g1))
rec("BEFORE_WRITE", "1")
time.sleep(0.8)  # 给驱动 mark 时间
ok2, nw = write_bytes(h_out, b"a\\nb")
rec("WROTE", str(int(ok2)) + " " + str(nw))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            log = s.log()
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            v1 = s.wait_result(NAME, "SET_GET", timeout=15.0)
            if not v1:
                print("  [FAIL] SET_GET: 无结果")
                failures += 1
            else:
                parts = v1.split()
                if len(parts) == 2 and parts[0] == "1" and int(parts[1], 16) == 0x5:
                    print("  [PASS] SET_GET Set(0x1) 后 Get=0x5（强制保留 VT）")
                else:
                    print("  [FAIL] SET_GET: {}（期望 1 0x5）".format(v1))
                    failures += 1

            v2 = s.wait_result(NAME, "BEFORE_WRITE", timeout=15.0)
            if not v2:
                print("  [FAIL] BEFORE_WRITE: 无结果")
                failures += 1
            log.mark()
            s.wait_result(NAME, "DONE", timeout=15.0)

            # 裸 LF 归一：61 0D 0A 62
            m = log.wait_for_regex(
                r"ChildVtOutput: len=4 written=4 ok=1 err=0 hex\[4\]=61 0D 0A 62",
                timeout=8.0)
            if m:
                print("  [PASS] VT 直通裸 LF 归一 (61 0D 0A 62)")
            else:
                print("  [FAIL] VT 直通: 日志未见 61 0D 0A 62（8s 超时）")
                failures += 1

            # 反向断言：不得出现裸 LF 原样（61 0A 62）
            content = log.read_all()
            if "hex[3]=61 0A 62" in content:
                print("  [FAIL] 线上出现裸 LF 原样 61 0A 62（未归一）")
                failures += 1
            else:
                print("  [PASS] 线上无裸 LF 原样 (61 0A 62 缺席)")
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
