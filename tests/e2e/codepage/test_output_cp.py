"""特性: GetConsoleOutputCP/SetConsoleOutputCP + chcp    类别: codepage

链路: 目标 SetConsoleOutputCP → DLL Hook 缓存 + CpChange →
      目标 GetConsoleOutputCP → 返回缓存；chcp 命令同样走 Hook

预期（ModeHooks.cpp Phase 8，必须 PASS）:
  - 初始 GetConsoleOutputCP == 65001
  - SetConsoleOutputCP(936) 后 Get == 936
  - chcp 936 命令后 Get == 936（chcp.exe 调 SetConsoleOutputCP 被 Hook）
  - 恢复 65001

验证方式: 目标内 ctypes 自检 + 目标 os.system("chcp 936") + 驱动解析日志
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "output_cp"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes
k32 = ctypes.windll.kernel32
cp0 = k32.GetConsoleOutputCP()
ok1 = k32.SetConsoleOutputCP(936)
cp1 = k32.GetConsoleOutputCP()
import os as _os
_os.system("chcp 936 >nul")
cp2 = k32.GetConsoleOutputCP()
ok3 = k32.SetConsoleOutputCP(65001)
cp3 = k32.GetConsoleOutputCP()
rec("RESULT", "{} {} {} {} {} {} {}".format(
    cp0, int(ok1), cp1, cp2, int(ok3), cp3, ""))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v = s.wait_result(NAME, "RESULT", timeout=20.0)
            if not v:
                print("  [FAIL] RESULT: 无结果")
                failures += 1
            else:
                parts = v.split()
                cp0, ok1, cp1, cp2, ok3, cp3 = (int(x) for x in parts[:6])
                print("  [INFO] 初始={} set936(ok={}) get={} chcp后={} 恢复(ok={}) get={}".format(
                    cp0, ok1, cp1, cp2, ok3, cp3))
                # 初始 CP 是系统 ANSI 码（ConPTY 初始值，如 936），不做硬编码断言
                if ok1 and cp1 == 936:
                    print("  [PASS] SetConsoleOutputCP(936) 后 Get == 936")
                else:
                    print("  [FAIL] set936: ok={} get={}（期望 1/936）".format(ok1, cp1))
                    failures += 1
                if cp2 == 936:
                    print("  [PASS] chcp 936 命令生效（chcp.exe 调 Hook 命中）")
                else:
                    print("  [FAIL] chcp 936 后 Get={}（期望 936）".format(cp2))
                    failures += 1
                if ok3 and cp3 == 65001:
                    print("  [PASS] 恢复 65001")
                else:
                    print("  [FAIL] 恢复: ok={} get={}".format(ok3, cp3))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
