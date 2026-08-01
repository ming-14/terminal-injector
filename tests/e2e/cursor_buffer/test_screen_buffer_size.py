"""特性: SetConsoleScreenBufferSize（缓冲尺寸 + WINDOW_BUFFER_SIZE_EVENT）    类别: cursor_buffer

链路:
  - 目标程序 SetConsoleScreenBufferSize → DLL 虚拟状态 dwSize 更新（Phase 14）
  - 测试进程改 WT 窗口尺寸 → mediator WtSizeWatcher 检测 resize
    → DllRecvLoop EnqueueResizeEvent 注入 WINDOW_BUFFER_SIZE_EVENT → 目标程序读到

预期:
  - 返回 TRUE；Get dwSize 与设置值一致（虚拟状态更新）
  - 目标程序收到 WINDOW_BUFFER_SIZE_EVENT（mediator 侧 resize 注入，ENABLE_WINDOW_INPUT）
  - 事件尺寸与窗口 resize 后的缓冲尺寸一致（>= 窗口列行数）

验证方式: 目标程序自检（虚拟状态 + 输入事件队列 Peek 轮询）+ 测试进程改 WT 尺寸
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32gui

from common.session import TestSession
from common import result as result_mod
from helpers import injector

NAME = "screen_buffer_size"

NEW_W = 100
NEW_H = 40

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()
h_in = get_std_in()
set_mode(h_in, ENABLE_WINDOW_INPUT | ENABLE_EXTENDED_FLAGS)
_k.FlushConsoleInputBuffer(h_in)

ok = _k.SetConsoleScreenBufferSize(h_out, COORD({W}, {H}))
check("SET_SIZE_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
info = get_csbi(h_out)
check("SIZE_QUERY", info is not None and info.dwSize.X == {W} and info.dwSize.Y == {H},
      "dwSize=" + str((info.dwSize.X, info.dwSize.Y) if info else "?"))

got_event = False
ew = eh = -1
deadline = time.time() + 5.0
while time.time() < deadline and not got_event:
    recs = read_input_records(h_in, 4, peek=True)
    for r in recs:
        if r.EventType == WINDOW_BUFFER_SIZE_EVENT:
            got_event = True
            ew = r.WindowBufferSizeEvent.dwSize.X
            eh = r.WindowBufferSizeEvent.dwSize.Y
            rec("EVENT_SIZE", str(ew) + " " + str(eh))
            read_input_records(h_in, len(recs))
            break
    if not got_event:
        time.sleep(0.05)
check("EVENT_RECEIVED", got_event, "no WINDOW_BUFFER_SIZE_EVENT in 5s")
check("EVENT_SIZE_OK", got_event and ew >= 60 and eh >= 20,
      "event=(" + str(ew) + "," + str(eh) + ")" if got_event else "no event")
done()
'''.format(W=NEW_W, H=NEW_H)


def _wt_hwnd():
    try:
        hwnd = injector._test_wt_hwnd
    except AttributeError:
        hwnd = None
    if hwnd is None:
        hwnds = injector.find_wt_windows()
        if hwnds:
            hwnd = hwnds[-1]
    return hwnd


def _resize_wt(delta_w: int, delta_h: int) -> tuple:
    """改 WT 窗口尺寸（触发 mediator resize 同步），返回原始 (w, h)。"""
    hwnd = _wt_hwnd()
    if not hwnd:
        raise RuntimeError("no WT window found")
    rect = win32gui.GetWindowRect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    ctypes.windll.user32.SetWindowPos(
        hwnd, 0, rect[0], rect[1],
        max(w + delta_w, 400), max(h + delta_h, 300), 0)
    return w, h


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            v = s.wait_result(NAME, "SIZE_QUERY", timeout=10.0)
            if v == "PASS":
                print("  [PASS] SET_SIZE_RET (SIZE_QUERY 同批)")
            else:
                print("  [FAIL] SET_SIZE_RET/SIZE_QUERY: {}".format(v or "no result"))
                failures += 1
            orig = None
            try:
                orig = _resize_wt(120, 40)
                time.sleep(1.0)
                for key in ("EVENT_RECEIVED", "EVENT_SIZE_OK"):
                    v = s.wait_result(NAME, key, timeout=10.0)
                    if v == "PASS":
                        print("  [PASS] {}".format(key))
                    else:
                        print("  [FAIL] {}: {}".format(key, v or "no result"))
                        failures += 1
            except RuntimeError as e:
                print("  [FAIL] resize WT 失败: {}".format(e))
                failures += 1
            finally:
                if orig:
                    hwnd = _wt_hwnd()
                    if hwnd:
                        ctypes.windll.user32.SetWindowPos(
                            hwnd, 0, 0, 0, orig[0], orig[1], 0x0001 | 0x0002 | 0x0004)
                    time.sleep(1.0)
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
