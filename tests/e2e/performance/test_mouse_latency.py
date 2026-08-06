"""特性: 鼠标端到端延迟（点击 → 目标程序读到）    类别: performance

链路: SendInput 点击 → WT → ConPTY → mediator → DLL InputQueue → 目标
      ReadConsoleInputW（wait_input 阻塞等待，非轮询）

预期:
  - 50 次采样，延迟 P95 < 50ms（端到端含 WT 渲染进程与管道传输）
  - 无事件丢失：目标收到 50 个 down（与驱动发送次数一致）

时间基准: 双方均用 GetTickCount64（同一系统时钟，毫秒精度）

验证方式: 驱动记录每次点击前时间戳，目标记录每次事件收到时间戳，
          按发送顺序配对计算延迟
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "mouse_latency"

SAMPLES = 50
P95_LIMIT_MS = 50

TARGET_BODY = f'''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
_k.GetTickCount64.argtypes = []
_k.GetTickCount64.restype = ctypes.c_ulonglong
h_in = get_std_in()
set_mode(h_in, ENABLE_MOUSE_INPUT)
h_wait = get_input_wait_handle()
rec("READY2", "1")
lats = []
first_done = False
deadline = time.time() + 40.0
while len(lats) < {SAMPLES} and time.time() < deadline:
    if wait_input(h_wait, 1000):
        n = wintypes.DWORD(0)
        if _k.GetNumberOfConsoleInputEvents(h_in, ctypes.byref(n)) and n.value > 0:
            for r in read_input_records(h_in, 16):
                if r.EventType == MOUSE_EVENT and r.MouseEvent.dwButtonState == 0x1:
                    # 第一个 down 为驱动预热（不计入样本），之后才开始计时
                    if not first_done:
                        first_done = True
                        rec("FIRST", "1")
                    else:
                        lats.append(_k.GetTickCount64())
rec("COUNT", str(len(lats)))
for _i, _t in enumerate(lats):
    rec("LAT" + str(_i + 1), str(_t))
done()
'''


def _tick() -> int:
    """GetTickCount64（与目标同源时钟）。"""
    try:
        return ctypes.windll.kernel32.GetTickCount64()
    except AttributeError:
        return int(time.time() * 1000)


def _lat_click(x: int, y: int) -> None:
    """发送左键 down/up，测量点在 down 紧邻处（绕开 mouse_click 的 50ms sleep）。

    t_send 由调用方在 _send(down) 前记录；这里只负责把 down 发出去。
    返回 down 已入 SendInput 队列后的时刻由调用方处理。
    """
    import ctypes as _ct
    from helpers import input_sim
    norm = input_sim._normalize_coords(x, y)

    def mk(flags):
        i = input_sim.INPUT()
        i.type = input_sim.INPUT_MOUSE
        i.mi.dx, i.mi.dy = norm
        i.mi.mouseData = 0
        i.mi.dwFlags = flags | input_sim.MOUSEEVENTF_ABSOLUTE
        i.mi.time = 0
        i.mi.dwExtraInfo = _ct.pointer(wintypes.ULONG(0))
        return i

    input_sim._send(mk(input_sim.MOUSEEVENTF_MOVE))
    time.sleep(0.02)
    input_sim._send(mk(input_sim.MOUSEEVENTF_LEFTDOWN))
    time.sleep(0.02)
    input_sim._send(mk(input_sim.MOUSEEVENTF_LEFTUP))


def _send_down(x: int, y: int) -> None:
    """仅发送左键 down（测量点在调用前记录）。"""
    import ctypes as _ct
    from helpers import input_sim
    norm = input_sim._normalize_coords(x, y)
    i = input_sim.INPUT()
    i.type = input_sim.INPUT_MOUSE
    i.mi.dx, i.mi.dy = norm
    i.mi.mouseData = 0
    i.mi.dwFlags = input_sim.MOUSEEVENTF_LEFTDOWN | input_sim.MOUSEEVENTF_ABSOLUTE
    i.mi.time = 0
    i.mi.dwExtraInfo = _ct.pointer(wintypes.ULONG(0))
    input_sim._send(i)


def _send_up(x: int, y: int) -> None:
    """发送左键 up（down 后释放，供目标收 up 对）。"""
    import ctypes as _ct
    from helpers import input_sim
    norm = input_sim._normalize_coords(x, y)
    i = input_sim.INPUT()
    i.type = input_sim.INPUT_MOUSE
    i.mi.dx, i.mi.dy = norm
    i.mi.mouseData = 0
    i.mi.dwFlags = input_sim.MOUSEEVENTF_LEFTUP | input_sim.MOUSEEVENTF_ABSOLUTE
    i.mi.time = 0
    i.mi.dwExtraInfo = _ct.pointer(wintypes.ULONG(0))
    input_sim._send(i)


def _p95(values) -> int:
    """P95 分位（升序，95% 位置取整）。"""
    if not values:
        return 0
    s = sorted(values)
    idx = int(len(s) * 0.95) - 1
    return s[max(0, idx)]


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY",
                         ready_timeout=30.0)
            v = s.wait_result(NAME, "READY2", timeout=20.0)
            if not v:
                print("  [FAIL] READY2: 无结果")
                failures += 1
            else:
                time.sleep(0.5)
                cx, cy = s.wt_center()
                _lat_click(cx - 40, cy)  # 预热（WT 鼠标模式/渲染就绪）
                v_first = s.wait_result(NAME, "FIRST", timeout=20.0)
                if not v_first:
                    print("  [FAIL] FIRST: 预热点击未到达目标")
                    failures += 1
                else:
                    sends = []
                    for i in range(SAMPLES):
                        t0 = _tick()
                        _send_down(cx + (i % 5) * 8 - 16, cy)
                        sends.append(t0)
                        time.sleep(0.02)
                        _send_up(cx + (i % 5) * 8 - 16, cy)
                        time.sleep(0.08)

                vc = s.wait_result(NAME, "COUNT", timeout=30.0)
                if not vc:
                    print("  [FAIL] COUNT: 无结果")
                    failures += 1
                else:
                    got = int(vc)
                    lats = []
                    for i in range(1, got + 1):
                        tv = s.wait_result(NAME, "LAT{}".format(i), timeout=10.0)
                        if tv:
                            lats.append(int(tv))
                    diffs = []
                    for t_recv, t_send in zip(lats, sends):
                        d = t_recv - t_send
                        if 0 <= d < 10000:  # 过滤异常对（时钟翻转/错位）
                            diffs.append(d)
                    if len(diffs) < SAMPLES:
                        print("  [WARN] 有效样本 {}（期望 {}）".format(
                            len(diffs), SAMPLES))
                    p95 = _p95(diffs)
                    mx = max(diffs) if diffs else 0
                    avg = sum(diffs) / len(diffs) if diffs else 0
                    print("  [INFO] 样本 {}: P95={}ms 平均={:.1f}ms 最大={}ms"
                          .format(len(diffs), p95, avg, mx))
                    if got < SAMPLES:
                        print("  [FAIL] 目标收到 {} 个 down（期望 {}，事件丢失）"
                              .format(got, SAMPLES))
                        failures += 1
                    elif p95 > P95_LIMIT_MS:
                        print("  [FAIL] P95 {}ms > {}ms".format(p95, P95_LIMIT_MS))
                        failures += 1
                    else:
                        print("  [PASS] P95 {}ms <= {}ms".format(p95, P95_LIMIT_MS))
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
