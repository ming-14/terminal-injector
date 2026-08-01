"""特性: 模式切换重置滚动状态    类别: scrollback

链路: 目标 SetConsoleScreenBufferSize(1000) → bufferSize.Y=1000 →
      目标 SetConsoleMode 切换输入模式 → ModeHooks →
      VirtualConsoleState::ResetScrollback → bufferSize 恢复视口行数

预期（ModeHooks.cpp:147 Phase 18，必须 PASS）:
  - 设置 1000 后 dwSize.Y == 1000
  - 模式切换后 dwSize.Y == 屏幕行数（scrollback 与用户高度被重置）

验证方式: 目标 ctypes 自检（GetConsoleScreenBufferInfo 命中 DLL 缓存）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "mode_switch_reset"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes
k32 = ctypes.windll.kernel32
class CSI(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_short * 2),
                ("dwCursorPosition", ctypes.c_short * 2),
                ("wAttributes", ctypes.c_ushort),
                ("srWindow", ctypes.c_short * 4),
                ("dwMaximumWindowSize", ctypes.c_short * 2)]
class COORD2(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]
k32.GetConsoleScreenBufferInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(CSI)]
k32.GetConsoleScreenBufferInfo.restype = ctypes.c_int
k32.SetConsoleScreenBufferSize.argtypes = [ctypes.c_void_p, COORD2]
k32.SetConsoleScreenBufferSize.restype = ctypes.c_int
k32.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint]
k32.SetConsoleMode.restype = ctypes.c_int
h_out = get_std_out()
h_in = get_std_in()
info = CSI()
k32.GetConsoleScreenBufferInfo(h_out, ctypes.byref(info))
r0 = info.dwSize[1]
k32.SetConsoleScreenBufferSize(h_out, COORD2(120, 1000))
info2 = CSI()
k32.GetConsoleScreenBufferInfo(h_out, ctypes.byref(info2))
y1 = info2.dwSize[1]
# 切换输入模式（0x01 = ENABLE_PROCESSED_INPUT，不含 VT_INPUT）→ ResetScrollback
okm = k32.SetConsoleMode(h_in, 0x01)
time.sleep(0.5)
info3 = CSI()
k32.GetConsoleScreenBufferInfo(h_out, ctypes.byref(info3))
y2 = info3.dwSize[1]
# 恢复原始输入模式
k32.SetConsoleMode(h_in, 0x1e7)
rec("RESULT", "{} {} {} {} {}".format(r0, y1, int(okm), y2, ""))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v = s.wait_result(NAME, "RESULT", timeout=15.0)
            if not v:
                print("  [FAIL] RESULT: 无结果")
                failures += 1
            else:
                r0, y1, okm, y2 = (int(x) for x in v.split()[:4])
                print("  [INFO] 屏幕行 R={} 设1000后Y={} 切换模式(ok={})后Y={}".format(
                    r0, y1, okm, y2))
                if r0 > 0:
                    print("  [PASS] 初始屏幕行 R={}".format(r0))
                else:
                    print("  [FAIL] 初始 R={}".format(r0))
                    failures += 1
                if y1 == 1000:
                    print("  [PASS] 设置用户高度 1000 后 dwSize.Y=1000")
                else:
                    print("  [FAIL] 设 1000 后 Y={}（期望 1000）".format(y1))
                    failures += 1
                if okm and y2 == r0:
                    print("  [PASS] 模式切换后 dwSize.Y={}（scrollback/用户高度已重置）".format(y2))
                else:
                    print("  [FAIL] 切换后 ok={} Y={}（期望 1/{}）".format(okm, y2, r0))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
