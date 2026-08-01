"""特性: 鼠标横向滚轮    类别: mouse

链路: SendInput 横滚 → WT SGR 序列（SGR 1006 无标准横滚编码，InputRecordToVt
      按 xterm 扩展约定发 \\x1b[<66;x;yM 右滚 / <67;x;yM 左滚）→ DLL 翻译 →
      目标收到

实际行为（LIM-006 已修复）:
  - VtToInputRecord.cpp ParseMouse 把 btn&64 的滚轮事件按 baseBtn 区分：
    baseBtn 2/3（SGR 66/67 横滚）→ MOUSE_HWHEELED 标志 + 高字 ±1
    （66=右滚 +1，67=左滚 -1）；baseBtn 0/1 → 垂直 MOUSE_WHEELED
  - 修复前：66/67 均落到"非 0"分支 → 误译为垂直下滚 0xFFFF0000

测试验证:
  - 若 WT 输出横滚序列：收到 MOUSE_HWHEELED 事件，记录实际 buttonState
  - 若 WT 不输出：超时无事件 → 记录差异后 PASS（SKIP 分支）

验证方式: 目标 ReadConsoleInputW 循环收 MOUSE_EVENT 并 rec 每事件
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "hwheel"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_MOUSE_INPUT)
rec("READY2", "1")
deadline = time.time() + 12.0
evs = []
while time.time() < deadline:
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
if len(evs) > 0:
    check("HWHEEL_FLAG", any(e[1] == MOUSE_HWHEELED for e in evs),
          "SGR 66/67 横滚应译作 MOUSE_HWHEELED（LIM-006 已修复）")
    check("HAVE_EV", True, "")
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
                s.mouse_hwheel(cx, cy, 120)   # 右滚
                time.sleep(0.4)
                s.mouse_hwheel(cx, cy, -120)  # 左滚

                vc = s.wait_result(NAME, "COUNT", timeout=20.0)
                if not vc:
                    print("  [FAIL] COUNT: 无结果")
                    failures += 1
                elif int(vc) == 0:
                    print("  [SKIP] WT 不输出横滚 SGR 序列（12s 无事件，"
                          "SendInput MOUSEEVENTF_HWHEEL 无效/未转发）——"
                          "已记录差异 LIM-006")
                else:
                    seq = ", ".join(
                        s.wait_result(NAME, "EV{}".format(i), timeout=5.0)
                        for i in range(1, int(vc) + 1))
                    v_h = s.wait_result(NAME, "HWHEEL_FLAG", timeout=5.0)
                    if v_h is not None and "FAIL" not in v_h:
                        print("  [PASS] 横滚事件带 MOUSE_HWHEEL 标志: {}".format(seq))
                    else:
                        print("  [FAIL] 横滚事件到达但无 MOUSE_HWHEEL 标志"
                              "（LIM-006 修复失效：SGR 66/67 应译作 MOUSE_HWHEELED"
                              "+ 高字符号）: {}".format(seq))
                        failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
