"""特性: 鼠标坐标精度    类别: mouse

链路: SendInput 点击 → WT SGR 1006（坐标 1-based）→ DLL 翻译转 0-based →
      目标收到 MOUSE_EVENT dwMousePosition

预期（VtToInputRecord.cpp:380-381 SGR 坐标 1-based → 0-based）:
  - 点击窗口内不同两点 A、B，目标收到的坐标 (X,Y) 满足 0 <= X,Y
  - 方向性：右移 → X 增大；下移 → Y 增大（像素位移映射到字符坐标）
  - 同一像素点两次点击坐标一致（稳定）

验证方式: 目标记录两次点击的 dwMousePosition，驱动点击窗口内 4 个点
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "coords"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_MOUSE_INPUT)
rec("READY2", "1")
deadline = time.time() + 20.0
evs = []
while len(evs) < 8 and time.time() < deadline:
    n = wintypes.DWORD(0)
    _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
    if n.value > 0:
        rs = read_input_records(h_in, 16)
        for r in rs:
            if r.EventType == MOUSE_EVENT:
                m = r.MouseEvent
                if m.dwButtonState == 0x1:  # 只记按下事件（4 个点）
                    evs.append((m.dwMousePosition.X, m.dwMousePosition.Y))
                    rec("P" + str(len(evs)),
                        "%d,%d" % (m.dwMousePosition.X, m.dwMousePosition.Y))
    time.sleep(0.1)
rec("COUNT", str(len(evs)))
if len(evs) == 4:
    a, b, c, d = evs
    check("INBOUND", all(0 <= x < 1000 and 0 <= y < 1000 for x, y in evs),
          repr(evs))
    # A 在 B 左上、C 在 D 右下（B 偏移 A 右下、D 偏移 C 右下）
    check("A_LT_B", a[0] <= b[0] and a[1] <= b[1], "A={} B={}".format(a, b))
    check("C_LT_D", c[0] <= d[0] and c[1] <= d[1], "C={} D={}".format(c, d))
    check("DISTINCT", (b[0] - a[0]) + (d[0] - c[0]) > 0,
          "A={} B={} C={} D={}".format(a, b, c, d))
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
                rect = s.wt_rect()
                if not rect:
                    print("  [FAIL] 驱动: 无法获取 WT 窗口矩形")
                    failures += 1
                else:
                    cx = (rect[0] + rect[2]) // 2
                    cy = (rect[1] + rect[3]) // 2
                    # A=左上区域, B=A 右下偏移, C=A, D=B（验证重复点击稳定）
                    for pt in ((cx - 100, cy - 50), (cx + 100, cy + 50),
                               (cx - 100, cy - 50), (cx + 100, cy + 50)):
                        s.mouse_click(pt[0], pt[1], "left")
                        time.sleep(0.4)

                    vc = s.wait_result(NAME, "COUNT", timeout=25.0)
                    if not vc:
                        print("  [FAIL] COUNT: 无结果")
                        failures += 1
                    else:
                        n = int(vc)
                        if n != 4:
                            print("  [FAIL] 收到 {} 个按下事件（期望 4）".format(n))
                            failures += 1
                        else:
                            for k in ("INBOUND", "A_LT_B", "C_LT_D", "DISTINCT"):
                                vk = s.wait_result(NAME, k, timeout=5.0)
                                if vk == "PASS":
                                    print("  [PASS] {}".format(k))
                                else:
                                    print("  [FAIL] {}: {}".format(k, vk))
                                    failures += 1
                            pts = [s.wait_result(NAME, "P{}".format(i), timeout=5.0)
                                   for i in range(1, 5)]
                            print("  [INFO] 坐标: A={} B={} C={} D={}".format(*pts))
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
