"""特性: ENABLE_PROCESSED_OUTPUT（换行处理）    类别: modes

链路: 目标 SetConsoleMode/WriteFile → DLL ModeHooks/WriteFile_Detour（VT 直通）

预期（工程实际语义）:
  - SetConsoleMode(输出句柄, PROCESSED_OUTPUT) → Get == 0x5（强制保留 VT_PROCESSING）
  - VT 直通模式下 WriteFile 写 "a\\nb" 字节原样直通（\\n 不转 CRLF）

与原生语义的差异（架构决定，VT 直通）:
  - 原生 ConHost：PROCESSED_OUTPUT 开时 WriteFile 写 \\n 转 CRLF（0A→0D 0A）
  - 工程：输出模式恒强制 VT_PROCESSING，WriteFile 字节原样转发（ConPTY 侧处理
    \\n），故 \\n 保持 0A。此差异由 VT 直通架构决定（见 OutputHooks.cpp:252）

验证方式: 目标自检 + mediator 日志 ChildVtOutput 字节
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
            m = log.wait_for_regex(
                r"ChildVtOutput: len=3 written=3 ok=1 err=0 hex\[3\]=61 0A 62",
                timeout=8.0)
            if m:
                print("  [PASS] VT 直通 \\\\n 原样 (61 0A 62)，不转 CRLF")
            else:
                print("  [FAIL] VT 直通: 日志未见 61 0A 62（8s 超时）")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
