"""特性: 用户缓冲区高度保留    类别: scrollback

链路: 目标 SetConsoleScreenBufferSize(120, 1000) → BufferHooks →
      VirtualConsoleState::SetUserBufferHeight(1000) →
      bufferSize.Y = max(rows, 1000, rows+scrollback) → 目标读
      GetConsoleScreenBufferInfo dwSize.Y == 1000

预期（Phase 18 语义，必须 PASS）:
  - 设置后 dwSize.Y == 1000（用户高度立即反映到 DLL 缓存）
  - DLL 日志（injected_<pid>.log）含 SetUserBufferHeight height=1000

验证方式: 目标 ctypes 自检 + 上报自身 PID 供驱动读 DLL 日志
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "user_buffer_height"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes, os as _os
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
h_out = get_std_out()
info = CSI()
k32.GetConsoleScreenBufferInfo(h_out, ctypes.byref(info))
y0 = info.dwSize[1]
ok = k32.SetConsoleScreenBufferSize(h_out, COORD2(120, 1000))
info2 = CSI()
k32.GetConsoleScreenBufferInfo(h_out, ctypes.byref(info2))
y1 = info2.dwSize[1]
rec("RESULT", "{} {} {} {}".format(y0, int(ok), y1, _os.getpid()))
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
                parts = v.split()
                y0, ok, y1 = (int(x) for x in parts[:3])
                pid = parts[3]
                print("  [INFO] 初始 Y={} set(ok={}) 后 Y={}".format(y0, ok, y1))
                if y0 > 0:
                    print("  [PASS] 初始屏幕行 Y={}".format(y0))
                else:
                    print("  [FAIL] 初始 Y={}".format(y0))
                    failures += 1
                if ok and y1 == 1000:
                    print("  [PASS] SetConsoleScreenBufferSize(120,1000) 后 dwSize.Y=1000")
                else:
                    print("  [FAIL] set: ok={} Y={}（期望 1/1000）".format(ok, y1))
                    failures += 1
                log_path = r"C:\temp\injected_{}.log".format(pid)
                deadline = time.time() + 6.0
                m = None
                while time.time() < deadline:
                    if os.path.exists(log_path):
                        with open(log_path, "r", encoding="utf-8",
                                  errors="ignore") as f:
                            c = f.read()
                        m = re.search(
                            r"SetUserBufferHeight: height=(\d+) bufferSize.Y=(\d+)",
                            c)
                        if m:
                            break
                    time.sleep(0.3)
                if m and int(m.group(1)) == 1000:
                    print("  [PASS] DLL 日志 SetUserBufferHeight height=1000 bufferSize.Y={}".format(
                        m.group(2)))
                else:
                    print("  [FAIL] 日志缺失 SetUserBufferHeight height=1000")
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
