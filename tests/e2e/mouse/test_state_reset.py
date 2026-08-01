"""特性: 模式切换后鼠标按键状态重置    类别: mouse

链路: 按下→模式切换→再按下：DLL 的 SetConsoleMode hook 在模式变化时调用
      ConsoleState::SetMouseButtonState(0)（ModeHooks.cpp），
      VtToInputRecord 跨事件按键状态据此清零

预期:
  - 第一次点击:  按下 0x1 → 释放 0x0
  - 切换模式后第二次点击: 按下 0x1 → 释放 0x0（无残留/累积按键位）

验证方式: 目标切换模式两次（触发状态重置），驱动点击两次比对事件
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "state_reset"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_MOUSE_INPUT)
rec("READY2", "1")
# 第一次点击（驱动触发）
deadline = time.time() + 15.0
evs = []
while len(evs) < 2 and time.time() < deadline:
    n = wintypes.DWORD(0)
    _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
    if n.value > 0:
        rs = read_input_records(h_in, 16)
        for r in rs:
            if r.EventType == MOUSE_EVENT:
                m = r.MouseEvent
                evs.append(m.dwButtonState)
                rec("EV" + str(len(evs)), "%08x" % m.dwButtonState)
    time.sleep(0.1)
rec("COUNT1", str(len(evs)))
# 切换模式两次（触发 ConsoleState 鼠标状态重置）
set_mode(h_in, ENABLE_MOUSE_INPUT | ENABLE_ECHO_INPUT)
set_mode(h_in, ENABLE_MOUSE_INPUT)
rec("READY3", "1")
# 第二次点击（驱动触发）
evs2 = []
deadline = time.time() + 15.0
while len(evs2) < 2 and time.time() < deadline:
    n = wintypes.DWORD(0)
    _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
    if n.value > 0:
        rs = read_input_records(h_in, 16)
        for r in rs:
            if r.EventType == MOUSE_EVENT:
                m = r.MouseEvent
                evs2.append(m.dwButtonState)
                rec("EV2" + str(len(evs2)), "%08x" % m.dwButtonState)
    time.sleep(0.1)
rec("COUNT2", str(len(evs2)))
if len(evs) == 2 and len(evs2) == 2:
    check("FIRST", evs[0] == 0x1 and evs[1] == 0x0,
          ",".join(hex(e) for e in evs))
    check("SECOND", evs2[0] == 0x1 and evs2[1] == 0x0,
          ",".join(hex(e) for e in evs2))
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
                vc1 = s.wait_result(NAME, "COUNT1", timeout=20.0)
                if not vc1:
                    print("  [FAIL] COUNT1: 无结果")
                    failures += 1
                elif int(vc1) != 2:
                    print("  [FAIL] 第一次点击收到 {} 事件（期望 2）".format(vc1))
                    failures += 1
                else:
                    print("  [PASS] 第一次点击 2 事件 ({}, {})".format(
                        s.wait_result(NAME, "EV1", timeout=5.0),
                        s.wait_result(NAME, "EV2", timeout=5.0)))

                v3 = s.wait_result(NAME, "READY3", timeout=20.0)
                if not v3:
                    print("  [FAIL] READY3: 无结果（模式切换未完成）")
                    failures += 1
                else:
                    time.sleep(0.5)
                    s.mouse_click(cx, cy, "left")
                    vc2 = s.wait_result(NAME, "COUNT2", timeout=20.0)
                    v_f = s.wait_result(NAME, "FIRST", timeout=5.0)
                    v_s = s.wait_result(NAME, "SECOND", timeout=5.0)
                    if vc2 and int(vc2) == 2:
                        print("  [PASS] 第二次点击 2 事件 ({}, {})".format(
                            s.wait_result(NAME, "EV21", timeout=5.0),
                            s.wait_result(NAME, "EV22", timeout=5.0)))
                    else:
                        print("  [FAIL] 第二次点击: COUNT2={} FIRST={} SECOND={}".format(
                            vc2, v_f, v_s))
                        failures += 1
                    if v_f != "PASS" or v_s != "PASS":
                        print("  [FAIL] 状态重置断言: FIRST={} SECOND={}".format(v_f, v_s))
                        failures += 1
                    elif vc2 and int(vc2) == 2:
                        print("  [PASS] 模式切换后按键状态重置干净（无残留位）")
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
