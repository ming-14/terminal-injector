"""特性: 鼠标拖拽（按住移动）    类别: mouse

链路: 目标 set ENABLE_MOUSE_INPUT → mediator 发 \\x1b[?1002h\\x1b[?1006h（1002=按钮
      事件含拖拽）→ SendInput 按下→移动→释放 → WT SGR 序列 → DLL 翻译 →
      目标收到 MOUSE_EVENT

实际行为（WT ConPTY 限制，已记录差异 LIM-005，2026-08-05 实测修正）:
  - WT 对按下期间移动不输出 FR1（MOUSE_MOVED），而是按当前按钮状态重复
    输出 FR0 按下事件（4 步移动 → 4 个重复 down，flags 恒为 0x0）
  - 未按键悬停移动不输出任何事件（SendInput MOVE/PostMessage/SetCursorPos
    均验证无效）
  - 因此 MOUSE_MOVED 标志在 WT 链路不可达，但拖拽按下态可观测（重复 down）

测试验证:
  - 拖拽 = down(0x1) → [拖拽按下态重复 down] → up(0x0)
  - 释放坐标反映终点（WT 跟踪了位置）：down/up 坐标差异 > 0

验证方式: 目标 ReadConsoleInputW 循环收 MOUSE_EVENT 并 rec 每事件
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "drag_move"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_MOUSE_INPUT)
rec("READY2", "1")
deadline = time.time() + 20.0
evs = []
while time.time() < deadline:
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
                if m.dwButtonState == 0x0:
                    rec("SEQ_DONE", "1")
                    break
    if any(e[0] == 0x0 for e in evs):
        break
    time.sleep(0.1)
rec("COUNT", str(len(evs)))
# 断言：down(0x1) → 拖拽按下态重复 down(0x1) → up(0x0)；位置有移动
if len(evs) >= 2 and evs[-1][0] == 0x0:
    check("DOWN", evs[0][0] == 0x1, hex(evs[0][0]))
    check("HOLD", all(e[0] == 0x1 for e in evs[:-1]),
          ",".join(hex(e[0]) for e in evs[:-1]))
    check("UP", evs[-1][0] == 0x0, hex(evs[-1][0]))
    dx = abs(evs[-1][2] - evs[0][2])
    dy = abs(evs[-1][3] - evs[0][3])
    check("MOVED_POS", dx + dy > 0, "down=({},{}) up=({},{})".format(
        evs[0][2], evs[0][3], evs[-1][2], evs[-1][3]))
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
                s.mouse_drag(cx - 120, cy, cx + 120, cy)

                vc = s.wait_result(NAME, "COUNT", timeout=25.0)
                if not vc:
                    print("  [FAIL] COUNT: 无结果")
                    failures += 1
                elif int(vc) < 2:
                    print("  [FAIL] 收到 {} 个事件（期望 >= 2: down/up；"
                          "WT 拖拽按下态以重复 down 呈现，见 LIM-005）"
                          .format(vc))
                    failures += 1
                else:
                    for k in ("DOWN", "HOLD", "UP", "MOVED_POS"):
                        vk = s.wait_result(NAME, k, timeout=5.0)
                        if vk == "PASS":
                            print("  [PASS] {}: {}".format(
                                k, {"DOWN": "down=0x1", "HOLD": "拖拽按下态保持",
                                    "UP": "up=0x0",
                                    "MOVED_POS": "坐标有移动"}[k]))
                        else:
                            print("  [FAIL] {}: {}".format(k, vk))
                            failures += 1
                    print("  [SKIP] MOUSE_MOVED 标志：WT ConPTY 拖拽只输出重复 down"
                          "（FR0），不出 FR1 移动事件，已记录差异 LIM-005")
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
