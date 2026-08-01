"""特性: WriteConsoleA + CP65001 中文 UTF-8 输出    类别: codepage

链路: 目标 WriteConsoleA(UTF-8 中文) → DLL A→W 转换（按缓存 outputCp=65001）
      → W 路径 → UTF-8 VT → mediator 日志

预期（OutputHooks.cpp，必须 PASS）:
  - mediator 日志 hex 含 "E4 B8 AD E6 96 87"（中文 UTF-8 字节）
  - 目标 WriteConsoleA 返回 TRUE

验证方式: 目标 ctypes 调用 + 驱动解析日志字节（子串搜索）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "utf8_output"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes
k32 = ctypes.windll.kernel32
k32.SetConsoleOutputCP(65001)
h_out = get_std_out()
buf = "中文UTF8".encode("utf-8")
written = ctypes.c_ulong(0)
ok = k32.WriteConsoleA(h_out, buf, len(buf), ctypes.byref(written), None)
rec("RESULT", "{} {} {}".format(int(ok), written.value, len(buf)))
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
                ok, written, total = (int(x) for x in parts[:3])
                # 注: written 按 W 字符数计数（中文每字 1 个 W 字符），与
                # UTF-8 字节数不等；仅要求写入成功且字节全量到达
                if ok and written > 0:
                    print("  [PASS] WriteConsoleA 返回 TRUE，W 写入 {} 字符（字节 {}）".format(
                        written, total))
                else:
                    print("  [FAIL] WriteConsoleA: ok={} written={}".format(ok, written))
                    failures += 1
                m = s.log().wait_for_regex(
                    r"E4 B8 AD E6 96 87 55 54 46 38", timeout=8.0)
                if m:
                    print("  [PASS] 日志含中文 UTF-8 字节 E4 B8 AD E6 96 87（UTF8）")
                else:
                    m2 = s.log().wait_for_regex(
                        r"E4 B8 AD E6 96 87", timeout=8.0)
                    if m2:
                        print("  [PASS] 日志含中文 UTF-8 字节 E4 B8 AD E6 96 87")
                    else:
                        print("  [FAIL] 日志缺失中文 UTF-8 字节")
                        failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
