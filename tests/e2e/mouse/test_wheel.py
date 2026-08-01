"""特性: 鼠标纵向滚轮    类别: mouse

链路: 目标 set ENABLE_MOUSE_INPUT → mediator 发 \\x1b[?1002h\\x1b[?1006h →
      SendInput 滚轮 → WT SGR 序列（\\x1b[<64;x;yM 上滚 / <65;x;yM 下滚）→ DLL
      翻译 MOUSE_WHEELED → 目标收到

预期:
  - 上滚 +120:   dwEventFlags=MOUSE_WHEELED(0x4), dwButtonState=0x00010000
  - 下滚 -120:   dwEventFlags=MOUSE_WHEELED(0x4), dwButtonState=0xFFFF0000
  （VtToInputRecord.cpp ParseMouse wheel 分支：baseBtn==0 → +0x10000，
    非 0 → 0xFFFF0000）

验证方式: 目标 ReadConsoleInputW 循环收 MOUSE_EVENT 并 rec 每事件
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "wheel"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_MOUSE_INPUT)
rec("READY2", "1")
deadline = time.time() + 20.0
evs = []
while len(evs) < 2 and time.time() < deadline:
    n = wintypes.DWORD(0)
    _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
    if n.value > 0:
        rs = read_input_records(h_in, 16)
        for r in rs:
            if r.EventType == MOUSE_EVENT:
                m = r.MouseEvent
                evs.append((m.dwButtonState, m.dwEventFlags))
                rec("EV" + str(len(evs)),
                    "%08x,%04x" % (m.dwButtonState, m.dwEventFlags))
    time.sleep(0.1)
rec("COUNT", str(len(evs)))
if len(evs) == 2:
    check("WHEEL_FLAG", evs[0][1] == MOUSE_WHEELED and evs[1][1] == MOUSE_WHEELED,
          "flags={},{}".format(hex(evs[0][1]), hex(evs[1][1])))
    check("WHEEL_UP", evs[0][0] == 0x00010000, hex(evs[0][0]))
    check("WHEEL_DOWN", evs[1][0] == 0xFFFF0000, hex(evs[1][0]))
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
                s.mouse_wheel(cx, cy, 120)   # 上滚
                time.sleep(0.4)
                s.mouse_wheel(cx, cy, -120)  # 下滚

                vc = s.wait_result(NAME, "COUNT", timeout=25.0)
                if not vc:
                    print("  [FAIL] COUNT: 无结果")
                    failures += 1
                elif int(vc) < 2:
                    print("  [FAIL] 收到 {} 个事件（期望 2）".format(vc))
                    failures += 1
                else:
                    for k in ("WHEEL_FLAG", "WHEEL_UP", "WHEEL_DOWN"):
                        vk = s.wait_result(NAME, k, timeout=5.0)
                        if vk == "PASS":
                            print("  [PASS] {}".format(k))
                        else:
                            print("  [FAIL] {}: {}".format(k, vk))
                            failures += 1
                    print("  [INFO] 事件: {}".format(
                        ", ".join(s.wait_result(NAME, "EV{}".format(i), timeout=5.0)
                                  for i in range(1, 3))))
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
