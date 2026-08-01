"""特性: Ctrl+C → SIGINT 信号传递（Phase 7）    类别: keyboard

链路: SendInput(Ctrl+C) → WT → mediator → DLL → 目标进程 SIGINT
      → python 信号处理器记录 SIGINT

预期:
  - 目标 python 死循环收到 SIGINT（signal.SIGINT == 2）
  - 信号到达后脚本正常退出（done）

验证方式: 目标脚本注册 SIGINT handler + 死循环，结果文件 SIGINT=<sig>
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from helpers import input_sim

NAME = "ctrl_c_signal"

TARGET_BODY = '''
rec("READY", "PASS")
import signal

def handler(sig, frame):
    rec("SIGINT", str(sig))
    done()
    sys.exit(0)

signal.signal(signal.SIGINT, handler)
while True:
    time.sleep(0.1)
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(1.0)
            input_sim.type_ctrl_c()
            v = s.wait_result(NAME, "SIGINT", timeout=15.0)
            if v == "2":
                print("  [PASS] SIGINT (收到 signal.SIGINT=2)")
            else:
                print("  [FAIL] SIGINT: {}".format(v or "超时未收到 SIGINT"))
                failures += 1
            if s.wait_done(NAME, timeout=5.0):
                print("  [PASS] DONE (信号后正常退出)")
            else:
                print("  [FAIL] DONE: 目标未退出")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
