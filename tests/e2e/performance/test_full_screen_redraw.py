"""特性: 满屏重绘性能（WriteConsoleOutputW 60fps 量级）    类别: performance

链路: 目标构造全屏 CHAR_INFO → WriteConsoleOutputW → DLL 翻译 VT →
      mediator → WT 渲染

预期:
  - N 帧全屏重绘全部成功（每帧 written == 全屏格数）
  - 总耗时 < 阈值：单帧端到端 < 50ms（60fps≈16.7ms/帧，50ms 留容差）
  - 无撕裂/无卡死：调用全部返回、耗时有限；日志有对应输出字节

验证方式: 目标侧耗时/成功数自检 + 驱动侧日志字节辅助
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from helpers import vt_capture

NAME = "full_screen_redraw"

FRAMES = 20
MAX_MS_PER_FRAME = 50

TARGET_BODY = f'''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
info = get_csbi(h_out)
if info is None:
    check("CSBI", False, "GetConsoleScreenBufferInfo failed")
    done()
    raise SystemExit(1)
# 满屏 = 窗口矩形尺寸（WriteConsoleOutput 目标区域）
w = info.srWindow.Right - info.srWindow.Left + 1
h = info.srWindow.Bottom - info.srWindow.Top + 1
full = w * h
rec("COLS", str(w))
rec("ROWS", str(h))
rec("FULL", str(full))
# 构造全屏 CHAR_INFO 数组（帧间字符不同，避免翻译器差分合并）
buf = (CHAR_INFO * full)()
size = COORD(w, h)
origin = COORD(0, 0)
rect = SMALL_RECT(0, 0, w - 1, h - 1)
ok = 0
t0 = time.time()
for i in range({FRAMES}):
    ch = chr(0x41 + i % 26)  # A-Z 循环，每帧不同字符
    for k in range(full):
        buf[k].Char = ch
        buf[k].Attributes = 0x07
    # WriteConsoleOutputW 无 written 参数：成功返回非零即整区域写入
    if _k.WriteConsoleOutputW(h_out, buf, size, origin, ctypes.byref(rect)):
        ok += 1
    else:
        rec("FAIL_FRAME", str(i))
        break
rec("TIME_MS", str(int((time.time() - t0) * 1000)))
rec("OK", str(ok))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY",
                         ready_timeout=30.0)
            v_ok = s.wait_result(NAME, "OK", timeout=30.0)
            v_time = s.wait_result(NAME, "TIME_MS", timeout=10.0)
            v_full = s.wait_result(NAME, "FULL", timeout=10.0)
            v_cols = s.wait_result(NAME, "COLS", timeout=10.0)
            v_rows = s.wait_result(NAME, "ROWS", timeout=10.0)

            if not v_ok or not v_time:
                print("  [FAIL] 目标未完成（疑似卡死）: OK={} TIME_MS={}"
                      .format(v_ok, v_time))
                failures += 1
            else:
                n_ok = int(v_ok)
                t_ms = int(v_time)
                print("  [INFO] {} 帧满屏重绘（{}x{}={} 格）: 成功 {} 帧, 耗时 {}ms"
                      .format(FRAMES, v_cols, v_rows, v_full, n_ok, t_ms))
                if n_ok != FRAMES:
                    print("  [FAIL] 成功 {} 帧（期望 {}，疑似卡死/失败）"
                          .format(n_ok, FRAMES))
                    failures += 1
                budget = FRAMES * MAX_MS_PER_FRAME
                if t_ms > budget:
                    print("  [FAIL] 总耗时 {}ms > 预算 {}ms（单帧 {}ms）"
                          .format(t_ms, budget, MAX_MS_PER_FRAME))
                    failures += 1
                else:
                    print("  [PASS] 耗时 {}ms <= 预算 {}ms（单帧 {}ms 平均 {:.1f}ms）"
                          .format(t_ms, budget, MAX_MS_PER_FRAME,
                                  t_ms / FRAMES))
                # 日志辅助：至少一帧完整输出的字节量到达（无卡死在链路上）
                total, _, packets = vt_capture.parse_child_vt_output(
                    s.log().read_all())
                if total >= int(v_full or 0):
                    print("  [PASS] 日志输出字节 {}（>= 单帧 {} 格，链路输出正常，"
                          "{} 包）".format(total, v_full, packets))
                else:
                    print("  [FAIL] 日志输出字节 {}（< 单帧 {} 格）"
                          .format(total, v_full))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
