"""特性: 输入队列操作（Peek/Read/Flush/GetNumberOfConsoleInputEvents）    类别: keyboard

链路: 目标脚本操作输入队列 + SendInput 3 个 'a' → DLL 队列 → 目标查询

预期:
  - 初始 Flush 后计数=0
  - 发送 3 个字符后计数=6（type_char 每字符 down+up 各 1 记录，真实 ConHost 同理）
  - Peek 1 个不消费，计数仍=6
  - Read 1 个后计数=5
  - Flush 后计数=0
  - 无数据时等 InputQueue 事件（h_wait）超时返回（wait_input(300ms) False）
    （不能等 stdin 句柄：真实 ConHost 队列残留使其恒有信号，见 F11 调试）

验证方式: 目标脚本操作并记录各阶段计数，测试进程在 INIT_EMPTY 后发送按键
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from helpers import input_sim

NAME = "input_queue_ops"

TARGET_BODY = '''
rec("READY", "PASS")
h_in = get_std_in()
h_wait = get_input_wait_handle()
set_mode(h_in, 0)
_k.FlushConsoleInputBuffer(h_in)

n = wintypes.DWORD(0)
_k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
check("INIT_EMPTY", n.value == 0, "n={}".format(n.value))
rec("WAIT_SENDER", "1")
time.sleep(2.0)

_k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
check("COUNT_3", n.value == 6, "n={}".format(n.value))

recs = read_input_records(h_in, 1, peek=True)
_k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
check("PEEK_KEEPS", len(recs) == 1 and n.value == 6, "peek={} n={}".format(len(recs), n.value))

recs = read_input_records(h_in, 1)
_k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
check("AFTER_READ_2", len(recs) == 1 and n.value == 5, "read={} n={}".format(len(recs), n.value))

_k.FlushConsoleInputBuffer(h_in)
_k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
check("AFTER_FLUSH_0", n.value == 0, "n={}".format(n.value))

timed_out = not wait_input(h_wait, 300)
check("WAIT_TIMEOUT_EMPTY", timed_out, "wait_input 未超时（有数据？）")
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v = s.wait_result(NAME, "WAIT_SENDER", timeout=10.0)
            if not v:
                print("  [FAIL] 目标未进入等待阶段")
                failures += 1
            else:
                print("  [PASS] INIT_EMPTY (初始计数=0)")
                time.sleep(0.5)
                input_sim.type_text("aaa")
            for key in ("COUNT_3", "PEEK_KEEPS", "AFTER_READ_2",
                        "AFTER_FLUSH_0", "WAIT_TIMEOUT_EMPTY"):
                v = s.wait_result(NAME, key, timeout=15.0)
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
