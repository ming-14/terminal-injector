"""特性: 鼠标双击    类别: mouse

链路: 目标 set ENABLE_MOUSE_INPUT → mediator 发 \\x1b[?1002h\\x1b[?1006h 给 WT →
      SendInput 快速两次点击 → WT SGR 序列 → DLL 翻译 → 目标收到 4 个事件

预期:
  - 两次点击 = 4 事件（down/up/down/up），buttonState 0x1 → 0x0 → 0x1 → 0x0
  - 已记录差异：SGR 1006 无双击概念，MOUSE_DOUBLE_CLICK 标志不设置
    （VtToInputRecord.cpp ParseMouse 无该标志逻辑）

验证方式: 目标 ReadConsoleInputW 循环收 MOUSE_EVENT 并 rec 每事件
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "double_click"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_MOUSE_INPUT)
rec("READY2", "1")
deadline = time.time() + 20.0
evs = []
while len(evs) < 4 and time.time() < deadline:
    n = wintypes.DWORD(0)
    _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
    if n.value > 0:
        rs = read_input_records(h_in, 16)
        for r in rs:
            if r.EventType == MOUSE_EVENT:
                m = r.MouseEvent
                evs.append((m.dwButtonState, m.dwEventFlags,
                            m.dwMousePosition.X, m.dwMousePosition.Y))
                rec("EV" + str(len(evs)),
                    "%08x,%04x" % (m.dwButtonState, m.dwEventFlags))
    time.sleep(0.1)
rec("COUNT", str(len(evs)))
if len(evs) == 4:
    check("SEQ", evs[0][0] == 0x1 and evs[1][0] == 0x0 and
                evs[2][0] == 0x1 and evs[3][0] == 0x0,
          ",".join(hex(e[0]) for e in evs))
    check("NO_DBL", evs[0][1] != MOUSE_DOUBLE_CLICK and
                    evs[2][1] != MOUSE_DOUBLE_CLICK,
          "dbl-click flag 不应出现（未实现）")
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
                s.mouse_click(cx, cy, "left")  # 连续两次 = 双击

                vc = s.wait_result(NAME, "COUNT", timeout=25.0)
                if not vc:
                    print("  [FAIL] COUNT: 无结果")
                    failures += 1
                else:
                    seq = ", ".join(
                        s.wait_result(NAME, "EV{}".format(i), timeout=5.0)
                        for i in range(1, 5)) if int(vc) == 4 else "-"
                    v_seq = s.wait_result(NAME, "SEQ", timeout=5.0)
                    if v_seq == "PASS":
                        print("  [PASS] 双击=4 事件, buttonState 0x1→0→0x1→0 ({})".format(seq))
                    else:
                        print("  [FAIL] 双击事件序列异常: {} ({})".format(v_seq, seq))
                        failures += 1
                    v_nd = s.wait_result(NAME, "NO_DBL", timeout=5.0)
                    if v_nd == "PASS":
                        print("  [SKIP] MOUSE_DOUBLE_CLICK 标志未实现（SGR 无双击概念，"
                              "已记录差异）——事件本身正确")
                    else:
                        print("  [FAIL] NO_DBL: {}".format(v_nd))
                        failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
