"""特性: Attach/Free/Alloc/GetConsoleWindow 静默拦截    类别: lifecycle

链路: 目标调 AllocConsole/AttachConsole/FreeConsole/GetConsoleWindow →
      ProtectionHooks 拦截 → 注入状态不受影响

预期（Phase 9，必须 PASS）:
  - AllocConsole == FALSE（ERROR_NOT_ENOUGH_MEMORY 8）
  - AttachConsole(ATTACH_PARENT_PROCESS) == FALSE（ERROR_ACCESS_DENIED 5）
  - FreeConsole == TRUE（假装成功）
  - GetConsoleWindow == 0（返回 NULL，隔离 ConHost 窗口操作）
  - 调用后 GetConsoleScreenBufferInfo 仍命中 DLL 缓存（dwSize 正常）

验证方式: 目标 ctypes 自检
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "self_protection"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes
k32 = ctypes.windll.kernel32
k32.AllocConsole.argtypes = []
k32.AllocConsole.restype = ctypes.c_int
k32.FreeConsole.argtypes = []
k32.FreeConsole.restype = ctypes.c_int
k32.AttachConsole.argtypes = [ctypes.c_uint]
k32.AttachConsole.restype = ctypes.c_int
k32.GetConsoleWindow.argtypes = []
k32.GetConsoleWindow.restype = ctypes.c_void_p
k32.GetLastError.restype = ctypes.c_uint
class CSI(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_short * 2),
                ("dwCursorPosition", ctypes.c_short * 2),
                ("wAttributes", ctypes.c_ushort),
                ("srWindow", ctypes.c_short * 4),
                ("dwMaximumWindowSize", ctypes.c_short * 2)]
k32.GetConsoleScreenBufferInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(CSI)]
k32.GetConsoleScreenBufferInfo.restype = ctypes.c_int
h_out = get_std_out()
ra = k32.AllocConsole()
ea = k32.GetLastError()
rb = k32.AttachConsole(0xFFFFFFFF)  # ATTACH_PARENT_PROCESS
eb = k32.GetLastError()
rf = k32.FreeConsole()
gcw = k32.GetConsoleWindow()
info = CSI()
okg = k32.GetConsoleScreenBufferInfo(h_out, ctypes.byref(info))
rec("RESULT", "{} {} {} {} {} {} {} {}".format(
    ra, ea, rb, eb, rf, int(gcw or 0), int(okg), info.dwSize[1]))
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
                ra, ea, rb, eb, rf, gcw, okg, y = (int(x) for x in v.split()[:8])
                print("  [INFO] Alloc={}/err={} Attach={}/err={} Free={} GCW={} GCSBI={}/Y={}".format(
                    ra, ea, rb, eb, rf, gcw, okg, y))
                if ra == 0 and ea == 8:
                    print("  [PASS] AllocConsole 被拦（FALSE + ERROR_NOT_ENOUGH_MEMORY）")
                else:
                    print("  [FAIL] AllocConsole: {} err={}（期望 0/8）".format(ra, ea))
                    failures += 1
                if rb == 0 and eb == 5:
                    print("  [PASS] AttachConsole 被拦（FALSE + ERROR_ACCESS_DENIED）")
                else:
                    print("  [FAIL] AttachConsole: {} err={}（期望 0/5）".format(rb, eb))
                    failures += 1
                if rf == 1:
                    print("  [PASS] FreeConsole 假装成功（TRUE，不断开控制台）")
                else:
                    print("  [FAIL] FreeConsole: {}（期望 1）".format(rf))
                    failures += 1
                if gcw == 0:
                    print("  [PASS] GetConsoleWindow 返回 NULL（隔离 ConHost 窗口）")
                else:
                    print("  [FAIL] GetConsoleWindow: {}（期望 0）".format(gcw))
                    failures += 1
                if okg and y > 0:
                    print("  [PASS] 调用后 GetConsoleScreenBufferInfo 仍命中缓存（Y={}）".format(y))
                else:
                    print("  [FAIL] 调用后 GCSBI: ok={} Y={}".format(okg, y))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
