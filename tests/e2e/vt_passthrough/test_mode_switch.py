"""特性: SetConsoleMode VT_INPUT 自动切换与回退    类别: vt_passthrough

链路: 目标 SetConsoleMode → DLL ModeHooks → ModeSwitchNotify → mediator 日志；
      目标 ReadFile（os.read）→ DLL ReadFile_Detour（透传分支读 raw 队列）

预期:
  - 开启 VT_INPUT：get 一致 + mediator 收到 ModeSwitchNotify (vtInput=1)
  - 开启后输入字符经 raw 队列直达 ReadFile（os.read 读到原始字节）
  - 清除 VT_INPUT：get 一致 + mediator 收到 ModeSwitchNotify (vtInput=0)

验证方式: 目标自检 + mediator 日志 + 输入字节往返
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "mode_switch"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
rec("BEFORE_SWITCH", "1")
time.sleep(0.8)  # 给驱动 mark 时间（ModeSwitchNotify 在 set 时同步发出）
# 开启 VT 输入透传
ok1 = set_mode(h_in, ENABLE_VIRTUAL_TERMINAL_INPUT)
g1 = get_mode(h_in)
rec("ON_GET", str(int(ok1)) + " " + hex(g1))
rec("READY_READ", "1")
# 透传模式：ReadFile 从 raw 队列读原始字节
import os as _os
b = _os.read(0, 8)
rec("GOT_RAW", b.hex())
# 清除 VT 输入，回行编辑
ok2 = set_mode(h_in, ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)
g2 = get_mode(h_in)
rec("OFF_GET", str(int(ok2)) + " " + hex(g2))
time.sleep(0.5)
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

            # mark 前只等 BEFORE_SWITCH（rec 在 set 之前，早于通知日志）
            v0 = s.wait_result(NAME, "BEFORE_SWITCH", timeout=15.0)
            if not v0:
                print("  [FAIL] BEFORE_SWITCH: 无结果")
                failures += 1
            log.mark()

            v1 = s.wait_result(NAME, "ON_GET", timeout=15.0)
            if not v1:
                print("  [FAIL] ON_GET: 无结果")
                failures += 1
            else:
                parts = v1.split()
                if len(parts) == 2 and parts[0] == "1" and int(parts[1], 16) == 0x200:
                    print("  [PASS] ON_GET Set(0x200) 后 Get 一致 (0x200)")
                else:
                    print("  [FAIL] ON_GET: {}（期望 1 0x200）".format(v1))
                    failures += 1

            # mediator 收到切换通知（vtInput=1）
            m1 = log.wait_for_regex(r"ModeSwitchNotify vtInput=1", timeout=8.0)
            if m1:
                print("  [PASS] 开启通知 mediator (ModeSwitchNotify vtInput=1)")
            else:
                print("  [FAIL] 开启通知: 日志未见 ModeSwitchNotify vtInput=1")
                failures += 1

            # 透传输入：目标 READY_READ 后输入 "a"，ReadFile 从 raw 队列读到 61
            v2 = s.wait_result(NAME, "READY_READ", timeout=10.0)
            if not v2:
                print("  [FAIL] READY_READ: 无结果")
                failures += 1
            s.type_text("a")
            v3 = s.wait_result(NAME, "GOT_RAW", timeout=15.0)
            if not v3:
                print("  [FAIL] GOT_RAW: 无结果（透传输入未到达）")
                failures += 1
            elif v3 == "61":
                print("  [PASS] 透传输入：os.read 读到原始字节 61")
            else:
                print("  [FAIL] GOT_RAW: {}（期望 61）".format(v3))
                failures += 1

            # 清除：回行编辑 + 通知 vtInput=0
            v4 = s.wait_result(NAME, "OFF_GET", timeout=15.0)
            if not v4:
                print("  [FAIL] OFF_GET: 无结果")
                failures += 1
            else:
                parts = v4.split()
                if len(parts) == 2 and parts[0] == "1" and int(parts[1], 16) == 0x7:
                    print("  [PASS] OFF_GET 清除 VT_INPUT 后 Get=0x7")
                else:
                    print("  [FAIL] OFF_GET: {}（期望 1 0x7）".format(v4))
                    failures += 1
            m2 = log.wait_for_regex(r"ModeSwitchNotify vtInput=0", timeout=8.0)
            if m2:
                print("  [PASS] 清除通知 mediator (ModeSwitchNotify vtInput=0)")
            else:
                print("  [FAIL] 清除通知: 日志未见 ModeSwitchNotify vtInput=0")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
