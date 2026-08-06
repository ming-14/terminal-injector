"""特性: ENABLE_WINDOW_INPUT（窗口缓冲区尺寸事件）    类别: modes

链路: 调整 WT 窗口尺寸 → mediator WtSizeWatcher → ResizeNotify → DLL
      EnqueueResizeEvent → 目标 ReadConsoleInputW 收到 WINDOW_BUFFER_SIZE_EVENT

预期（2026-08-05 实证修正）:
  - ENABLE_WINDOW_INPUT 开启：调整 WT 窗口尺寸，目标收到 WINDOW_BUFFER_SIZE_EVENT
  - 关闭后：调整窗口尺寸，目标**仍会收到**该事件——
    真实 ConPTY（RS5+）对 viewport 变化无条件发送 WINDOW_BUFFER_SIZE_EVENT，
    不受 ENABLE_WINDOW_INPUT 门控（microsoft/terminal#281 维护者确认：
    "send the events for all viewport changes regardless"；官方文档描述
    与实现不符，微软任务 19686633 待修文档）。DLL 无条件注入与该行为一致。

验证方式: 目标 ReadConsoleInputW 自检 + 驱动调整 WT 窗口尺寸
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "window_input"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_in = get_std_in()
set_mode(h_in, ENABLE_WINDOW_INPUT)
rec("READY2", "1")
# 循环收事件：出现 WINDOW_BUFFER_SIZE_EVENT 即记录
deadline = time.time() + 12.0
got = False
while time.time() < deadline:
    n = wintypes.DWORD(0)
    _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
    if n.value > 0:
        evs = read_input_records(h_in, 16)
        for ev in evs:
            if ev.EventType == WINDOW_BUFFER_SIZE_EVENT:
                rec("GOT_RESIZE", str(ev.WindowBufferSizeEvent.dwSize.X) + "x" + str(ev.WindowBufferSizeEvent.dwSize.Y))
                got = True
                break
        if got:
            break
    time.sleep(0.1)
if not got:
    rec("GOT_RESIZE", "TIMEOUT")
# 关闭 WINDOW_INPUT 后再收 3s：真实 ConPTY 对 viewport 变化无条件发送
# WINDOW_BUFFER_SIZE_EVENT（microsoft/terminal#281），关闭后仍应收到
set_mode(h_in, 0)
time.sleep(1.0)  # 等队列清空 + 事件处理
deadline2 = time.time() + 5.0
seen_off = False
while time.time() < deadline2:
    n = wintypes.DWORD(0)
    _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n))
    if n.value > 0:
        evs = read_input_records(h_in, 16)
        for ev in evs:
            if ev.EventType == WINDOW_BUFFER_SIZE_EVENT:
                seen_off = True
                break
    time.sleep(0.1)
rec("OFF_SEEN", str(int(seen_off)))
done()
'''


def _resize_wt() -> bool:
    """调整测试 WT 窗口尺寸（放大），触发 WtSizeWatcher。"""
    try:
        import win32gui
        import win32con
        from helpers import injector
        hwnd = injector._test_wt_hwnd
        if hwnd is None:
            hwnds = injector.find_wt_windows()
            if not hwnds:
                return False
            hwnd = hwnds[-1]
        rect = win32gui.GetWindowRect(hwnd)
        w = rect[2] - rect[0]
        h = rect[3] - rect[1]
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOP,
                              rect[0], rect[1], w + 120, h + 90,
                              win32con.SWP_NOACTIVATE)
        return True
    except Exception as e:  # noqa: BLE001
        print("  [INFO] resize 失败: {}".format(e))
        return False


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v_ready2 = s.wait_result(NAME, "READY2", timeout=20.0)
            if not v_ready2:
                print("  [FAIL] READY2: 无结果")
                failures += 1
            else:
                time.sleep(0.5)
                ok = _resize_wt()
                if not ok:
                    print("  [FAIL] 驱动: 无法调整 WT 窗口尺寸")
                    failures += 1
                v = s.wait_result(NAME, "GOT_RESIZE", timeout=20.0)
                if not v:
                    print("  [FAIL] GOT_RESIZE: 无结果（resize 事件未到达）")
                    failures += 1
                elif v == "TIMEOUT":
                    print("  [FAIL] GOT_RESIZE: 12s 未收到 WINDOW_BUFFER_SIZE_EVENT")
                    failures += 1
                else:
                    print("  [PASS] 开 WINDOW_INPUT 收到 resize 事件 ({})".format(v))

            # 关闭 WINDOW_INPUT 后再次 resize：真实 ConPTY 仍发 WINDOW_BUFFER_SIZE_EVENT
            # （viewport 变化无条件发送，不受 ENABLE_WINDOW_INPUT 门控，见文件头注释）
            time.sleep(1.0)
            ok = _resize_wt()
            v2 = s.wait_result(NAME, "OFF_SEEN", timeout=20.0)
            if not v2:
                print("  [FAIL] OFF_SEEN: 无结果")
                failures += 1
            elif v2 == "1":
                print("  [PASS] 关 WINDOW_INPUT 后仍收到 resize 事件（与真实 ConPTY 一致）")
            else:
                print("  [FAIL] OFF_SEEN: {}（关闭后未收到——真实 ConPTY 不受 WINDOW_INPUT 门控）".format(v2))
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
