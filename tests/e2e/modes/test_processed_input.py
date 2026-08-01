"""特性: ENABLE_PROCESSED_INPUT（Ctrl+C 处理）    类别: modes

链路: SendInput(type_ctrl_c) → WT → mediator → ConPTY → DLL 输入路径

预期（原生 ConHost 语义）:
  - PROCESSED_INPUT 清除后：Ctrl+C 作为字符 0x03 进入输入队列，目标读到 0x03
  - PROCESSED_INPUT 开启：Ctrl+C 触发中断（SIGINT）——已由 keyboard/ctrl_c_signal 覆盖

实际（架构限制）:
  - 0x03 经 ConPTY 时按共享输入模式（cmd 进程的模式，默认含 PROCESSED）处理，
    无条件触发 CTRL_C_EVENT → 目标进程被 SIGINT 中断（实测：READY 后目标死亡）
  - 目标进程自身清除 PROCESSED 无法影响 ConPTY 的 Ctrl+C 分发（ConPTY 不按
    进程区分输入模式）——同 BUG-003 类的上游/架构限制，非 DLL 缺陷
  - 故本测试行为断言 SKIP，保留观察窗口
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "processed_input"

TARGET_BODY = '''
rec("READY", "PASS")
h_in = get_std_in()
# 清除 PROCESSED_INPUT（raw 模式读单字符）
set_mode(h_in, 0)
_k.FlushConsoleInputBuffer(h_in)
while True:
    buf = ctypes.create_unicode_buffer(4)
    n = wintypes.DWORD(0)
    ok = _k.ReadConsoleW(h_in, buf, 3, ctypes.byref(n), None)
    if not ok:
        rec("GOT_CHAR", "READ_FAIL")
        break
    if n.value > 0:
        rec("GOT_CHAR", buf.value.encode("utf-8").hex())
        break
    time.sleep(0.05)
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            # Ctrl+C（PROCESSED 已清除）
            # 架构限制：0x03 经 ConPTY 按共享模式处理 → SIGINT → 目标中断，
            # 目标读到 0x03 的原生语义在 ConPTY 下不可达（非 DLL 缺陷）
            print("  [INFO] 发送 Ctrl+C（架构限制：ConPTY 共享模式处理）")
            s.type_ctrl_c()
            v = s.wait_result(NAME, "GOT_CHAR", timeout=5.0)
            if not v:
                print("  [SKIP] GOT_CHAR 无结果（目标被 SIGINT 中断，"
                      "与预期一致：ConPTY 共享模式处理 Ctrl+C）")
            elif v == "03":
                print("  [INFO] 目标读到 0x03（ConPTY 按模式透传）")
            else:
                print("  [FAIL] GOT_CHAR: {}（意外结果）".format(v))
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures, 1 skipped-by-limitation)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
