"""特性: python sys.stdin/os.read 原始字节（VT 透传）    类别: vt_passthrough

链路: SendInput(type_arrow) → WT → ConPTY（ESC 序列文本流）→ mediator →
      DLL VtInput（VT 模式入 raw 队列）→ 目标 os.read(0) 读原始字节

预期:
  - 目标开启 VT_INPUT 后，ReadFile 走透传分支（InputHooks.cpp:801 DequeueRaw）
  - 方向键 Up 输入以 ESC 序列 1B 5B 41 原样到达（非翻译为 KEY_EVENT）
  - 断言目标 os.read 读到的 hex == "1b5b41"

验证方式: 目标 os.read 自检 hex
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "raw_byte_read"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_VIRTUAL_TERMINAL_INPUT)
rec("READY_READ", "1")
# ReadFile 透传分支：读 raw 队列原始字节（含 ESC 序列）
import os as _os
b = _os.read(0, 8)
rec("GOT_RAW", b.hex())
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            v0 = s.wait_result(NAME, "READY_READ", timeout=15.0)
            if not v0:
                print("  [FAIL] READY_READ: 无结果")
                failures += 1
            else:
                # 输入方向键 Up → WT 转 ESC [ A 序列
                s.type_arrow("up")
                v = s.wait_result(NAME, "GOT_RAW", timeout=15.0)
                if not v:
                    print("  [FAIL] GOT_RAW: 无结果（透传字节未到达）")
                    failures += 1
                elif v.startswith("1b5b41"):
                    print("  [PASS] os.read 读到原始 ESC 序列 (1B 5B 41)")
                elif v == "":
                    print("  [FAIL] GOT_RAW: 空（读到了 NUL？）")
                    failures += 1
                else:
                    print("  [FAIL] GOT_RAW: {}（期望 1b5b41 开头）".format(v))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
