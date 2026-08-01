"""特性: 鼠标左/右/中键点击    类别: mouse

链路: 目标 set ENABLE_MOUSE_INPUT → mediator 发 \\x1b[?1002h\\x1b[?1006h 给 WT →
      SendInput 点击 → WT SGR 序列 → mediator → DLL VtToInputRecord 翻译
      MOUSE_EVENT → 目标 ReadConsoleInputW 收到

预期（按下→释放 两事件，buttonState）:
  - 左键:  0x1 (FROM_LEFT_1ST_BUTTON_PRESSED) → 0x0
  - 右键:  0x2 (RIGHTMOST_BUTTON_PRESSED)     → 0x0
  - 中键:  0x4 (FROM_LEFT_2ND_BUTTON_PRESSED) → 0x0

验证方式: 目标 ReadConsoleInputW 循环收 MOUSE_EVENT 并 rec 每事件
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "click"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_MOUSE_INPUT)
rec("READY2", "1")
# 收 6 个 MOUSE_EVENT（左/右/中各按下+释放），超时 20s
deadline = time.time() + 20.0
evs = []
while len(evs) < 6 and time.time() < deadline:
    n = wintypes.DWORD(0)
    _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
    if n.value > 0:
        rs = read_input_records(h_in, 16)
        for r in rs:
            if r.EventType == MOUSE_EVENT:
                m = r.MouseEvent
                evs.append((m.dwButtonState, m.dwEventFlags,
                            m.dwMousePosition.X, m.dwMousePosition.Y))
                rec("EV" + str(len(evs)), "%08x" % m.dwButtonState)
    time.sleep(0.1)
rec("COUNT", str(len(evs)))
if len(evs) >= 6:
    check("BTN_L_DOWN", evs[0][0] == 0x1, hex(evs[0][0]))
    check("BTN_L_UP", evs[1][0] == 0x0, hex(evs[1][0]))
    check("BTN_R_DOWN", evs[2][0] == 0x2, hex(evs[2][0]))
    check("BTN_R_UP", evs[3][0] == 0x0, hex(evs[3][0]))
    check("BTN_M_DOWN", evs[4][0] == 0x4, hex(evs[4][0]))
    check("BTN_M_UP", evs[5][0] == 0x0, hex(evs[5][0]))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v = s.wait_result(NAME, "READY2", timeout=20.0)
            if not v:
                print("  [FAIL] READY2: 无结果")
                failures += 1
            else:
                time.sleep(0.5)
                cx, cy = s.wt_center()
                s.mouse_click(cx, cy, "left")
                time.sleep(0.5)
                s.mouse_click(cx, cy, "right")
                time.sleep(0.5)
                s.mouse_click(cx, cy, "middle")

                vc = s.wait_result(NAME, "COUNT", timeout=25.0)
                if not vc:
                    print("  [FAIL] COUNT: 无结果")
                    failures += 1
                elif int(vc) < 6:
                    print("  [FAIL] 收到 {} 个事件（期望 6）".format(vc))
                    failures += 1
                else:
                    checks = ["BTN_L_DOWN", "BTN_L_UP", "BTN_R_DOWN",
                              "BTN_R_UP", "BTN_M_DOWN", "BTN_M_UP"]
                    exp = ["0x1", "0x0", "0x2", "0x0", "0x4", "0x0"]
                    for k, e in zip(checks, exp):
                        vk = s.wait_result(NAME, k, timeout=10.0)
                        if vk == "PASS":
                            print("  [PASS] {} buttonState={}".format(k, e))
                        else:
                            print("  [FAIL] {}: {}（期望 {}）".format(k, vk, e))
                            failures += 1
                    print("  [INFO] 事件序列: {}".format(
                        ", ".join(s.wait_result(NAME, "EV{}".format(i), timeout=5.0)
                                  for i in range(1, 7))))
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
