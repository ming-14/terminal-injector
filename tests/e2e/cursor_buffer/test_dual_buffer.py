"""特性: CreateConsoleScreenBuffer + SetActiveConsoleScreenBuffer（Alt Buffer）    类别: cursor_buffer

链路: 目标程序 CreateConsoleScreenBuffer（DLL 返回伪句柄，Phase 8）
      → SetConsoleActiveScreenBuffer(伪句柄) → DLL 发 ?1049h（进入 Alt Buffer）
      → 切回主句柄 → DLL 发 ?1049l（退出 Alt Buffer）

设计事实（BufferHooks.cpp）:
  - CreateConsoleScreenBuffer 不真创建，返回伪句柄魔数（0xABCDE123）
  - 伪句柄不可写（无真实缓冲）；切换方向由句柄是否 == 主缓冲句柄决定
  - 屏幕内容切换由 WT 原生 ?1049 支持，DLL 不自己保存/恢复

预期:
  - 创建返回非零伪句柄（非 INVALID_HANDLE_VALUE）
  - 激活伪句柄返回 TRUE，切回主句柄返回 TRUE，反复切换对称
  - 序列字节断言：日志含完整 ?1049h / ?1049l（BUG-002 已修复：
    kEnterAltBuffer/kExitAltBuffer 由 const char* 指针改为 char 数组，
    sizeof()-1 不再按指针大小截断尾字节）

验证方式: 目标程序自检 + mediator 日志字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "dual_buffer"

ENTER_HEX = "1B 5B 3F 31 30 34 39 68"   # ?1049h 进入 Alt Buffer
EXIT_HEX = "1B 5B 3F 31 30 34 39 6C"   # ?1049l 退出 Alt Buffer

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()

dw = wintypes.DWORD(0)
h2 = _k.CreateConsoleScreenBuffer(
    GENERIC_READ_WRITE, FILE_SHARE_READ_WRITE, None, CONSOLE_TEXTMODE_BUFFER,
    ctypes.byref(dw))
check("CREATE_RET", bool(h2) and h2 != 0xFFFFFFFF,
      "h2={} err=".format(h2) + str(ctypes.get_last_error()))

ok = _k.SetConsoleActiveScreenBuffer(h2)
check("ENTER_ALT_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
ok = _k.SetConsoleActiveScreenBuffer(h_out)
check("EXIT_ALT_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
ok = _k.SetConsoleActiveScreenBuffer(h2)
check("ENTER_AGAIN_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
ok = _k.SetConsoleActiveScreenBuffer(h_out)
check("EXIT_AGAIN_RET", bool(ok), "err=" + str(ctypes.get_last_error()))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("CREATE_RET", "ENTER_ALT_RET", "EXIT_ALT_RET",
                        "ENTER_AGAIN_RET", "EXIT_AGAIN_RET"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            content = s.log().read_all()
            ok_enter = "1B 5B 3F 31 30 34 39 68" in content
            ok_exit = "1B 5B 3F 31 30 34 39 6C" in content
            if ok_enter:
                print("  [PASS] LOG_ENTER_ALT (完整 ?1049h 序列)")
            else:
                print("  [FAIL] LOG_ENTER_ALT: 未观测到完整 ?1049h 序列")
                failures += 1
            if ok_exit:
                print("  [PASS] LOG_EXIT_ALT (完整 ?1049l 序列)")
            else:
                print("  [FAIL] LOG_EXIT_ALT: 未观测到完整 ?1049l 序列")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
