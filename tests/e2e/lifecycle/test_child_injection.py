"""特性: 子进程注入    类别: lifecycle

链路: cmd 启动 python 子进程 → ProcessHooks::OnChildProcessCreated 注入
      injected.dll → python 输出经 DLL→mediator 转发

预期（Phase 12）:
  - python 目标正常执行（rec 上报）
  - python WriteConsoleW 输出 "TI_SUB_96" → mediator 日志含其 UTF-8 字节

验证方式: 目标 WriteConsoleW + 上报自身 PID + 驱动解析日志
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "child_injection"

TEXT = "TI_SUB_96"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes
k32 = ctypes.windll.kernel32
k32.WriteConsoleW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                              ctypes.c_uint, ctypes.POINTER(ctypes.c_ulong),
                              ctypes.c_void_p]
k32.WriteConsoleW.restype = ctypes.c_int
h_out = get_std_out()
text = "TI_SUB_96"
wbuf = ctypes.create_unicode_buffer(text)
written = ctypes.c_ulong(0)
ok = k32.WriteConsoleW(h_out, wbuf, len(wbuf.value.encode("utf-16-le")) // 2,
                       ctypes.byref(written), None)
rec("RESULT", "{} {}".format(int(ok), written.value))
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
                print("  [FAIL] RESULT: 无结果（子进程注入失败或目标未执行）")
                failures += 1
            else:
                ok, written = (int(x) for x in v.split()[:2])
                if ok and written == len(TEXT):
                    print("  [PASS] python 子进程 WriteConsoleW 成功（注入后 DLL 工作）")
                else:
                    print("  [FAIL] WriteConsoleW: ok={} written={}".format(ok, written))
                    failures += 1
                hex_txt = " ".join("{:02X}".format(b) for b in TEXT.encode("utf-8"))
                m = s.log().wait_for_regex(hex_txt, timeout=10.0)
                if m:
                    print("  [PASS] 子进程输出经 DLL→mediator 转发（日志含 {}）".format(hex_txt))
                else:
                    print("  [FAIL] 日志未见 {} 字节（子进程输出未劫持）".format(hex_txt))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
