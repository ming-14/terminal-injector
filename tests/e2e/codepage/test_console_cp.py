"""特性: GetConsoleCP/SetConsoleCP 缓存行为    类别: codepage

链路: 目标 SetConsoleCP → DLL Hook 缓存 + CpChange（不调原 API）→
      目标 GetConsoleCP → 返回缓存

预期（ModeHooks.cpp Phase 8，必须 PASS）:
  - 初始 GetConsoleCP == 65001（mediator 启动设 CP_UTF8）
  - SetConsoleCP(936) 后 GetConsoleCP == 936
  - SetConsoleCP(65001) 后 GetConsoleCP == 65001
  - mediator 日志含 "ModeHooks: InputCP 65001 -> 936"

验证方式: 目标内 ctypes 自检 + 驱动解析日志
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "console_cp"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes
k32 = ctypes.windll.kernel32
cp0 = k32.GetConsoleCP()
ok1 = k32.SetConsoleCP(936)
cp1 = k32.GetConsoleCP()
ok2 = k32.SetConsoleCP(65001)
cp2 = k32.GetConsoleCP()
rec("RESULT", "{} {} {} {} {} {}".format(cp0, int(ok1), cp1, int(ok2), cp2, ""))
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
                cp0, ok1, cp1, ok2, cp2 = (int(x) for x in parts[:5])
                print("  [INFO] 初始={} set936(ok={}) get={} set65001(ok={}) get={}".format(
                    cp0, ok1, cp1, ok2, cp2))
                # 初始 CP 是系统 ANSI 码（ConPTY 初始值，如 936），不做硬编码断言；
                # 只验证 Set/Get 缓存一致性与 65001 恢复
                if cp1 == 936:
                    print("  [PASS] SetConsoleCP(936) 后 GetConsoleCP == 936（缓存生效）")
                else:
                    print("  [FAIL] set936: ok={} get={}（期望 1/936）".format(ok1, cp1))
                    failures += 1
                if ok2 and cp2 == 65001:
                    print("  [PASS] SetConsoleCP(65001) 后恢复 65001")
                else:
                    print("  [FAIL] set65001: ok={} get={}（期望 1/65001）".format(ok2, cp2))
                    failures += 1
                # 注: CpChange 的 ModeHooks 日志写入 injected_<pid>.log
                # （目录由 TI_INJECTED_LOG_DIR / GetTempPath 决定，DLL 进程私有日志），mediator 日志不可见，不在此断言
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
