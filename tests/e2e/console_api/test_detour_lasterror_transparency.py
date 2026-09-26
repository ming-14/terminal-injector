"""特性: Detour 对调用方 GetLastError 透明    类别: console_api

回归背景（2026-09-26）:
  IsConsoleHandle / IsInputHandleSlow 为判定句柄类型，会对**任意句柄**调
  GetFileType / GetNumberOfConsoleInputEvents。对非控制台句柄这些调用失败，并把
  **当前线程的 GetLastError 置为 ERROR_INVALID_HANDLE(6)**。
  而它们的调用点在 WriteFile / ReadFile / WaitForSingleObject(Ex) 等 Detour 内 ——
  这些 API 被应用高频调用（CPython 线程锁、asyncio 内部、文件 IO）→ 应用随后读自己的
  last-error 时读到我们留下的 6。
  实际症状：注入后运行 Textual（taskboard.py）→ asyncio 在 IocpProactor._poll 抛
  OSError [WinError 6] → 事件循环死亡 → TUI 画面定格（鼠标/定时器全部失效）。
  修复：HookCommon.h 新增 LastErrorGuard（RAII 保存/恢复 last-error），
        IsConsoleHandle 与 IsInputHandleSlow 各自加守卫。

预期:
  - 对**非控制台句柄**（事件）调用 WaitForSingleObject / WaitForMultipleObjects 后，
    调用方预先设置的 last-error 必须原样保留（Detour 对调用方透明）
  - 这两个 API 的返回值语义不受影响（0 超时 → WAIT_TIMEOUT）

验证方式: 目标进程内 ctypes 自检（设哨兵 last-error → 调用 → 读回比对）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "detour_lasterror"

# 哨兵值：故意用一个非 0、非 ERROR_INVALID_HANDLE 的值，便于区分"被污染"
SENTINEL = 0x5A5A

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入 / LazyInit 完成

import ctypes
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateEventW.restype = wintypes.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                             wintypes.LPCWSTR]
k32.WaitForSingleObject.restype = wintypes.DWORD
k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
k32.WaitForMultipleObjects.restype = wintypes.DWORD
k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD,
                                       ctypes.POINTER(wintypes.HANDLE),
                                       wintypes.BOOL, wintypes.DWORD]
k32.CloseHandle.argtypes = [wintypes.HANDLE]

SENTINEL = %d
WAIT_TIMEOUT = 0x102

# 非控制台句柄（事件）—— 应用会对这类句柄做等待；Detour 的句柄探测不得污染 last-error
hEv = k32.CreateEventW(None, False, False, None)
rec("HEVENT_OK", "1" if hEv else "0")

# 1) WaitForSingleObject（Phase 21 为 node/libuv 型 TUI 加的 Hook）
k32.SetLastError(SENTINEL)
r1 = k32.WaitForSingleObject(hEv, 0)
e1 = k32.GetLastError()
rec("WFSO_RET", r1)
rec("WFSO_ERR", e1)

# 2) WaitForMultipleObjects（Textual win32 driver 的 wait_for_handles 走这条）
arr = (wintypes.HANDLE * 1)(hEv)
k32.SetLastError(SENTINEL)
r2 = k32.WaitForMultipleObjects(1, arr, False, 0)
e2 = k32.GetLastError()
rec("WFMO_RET", r2)
rec("WFMO_ERR", e2)

k32.CloseHandle(hEv)
done()
''' % SENTINEL


def _check(s, key, expect_ret, expect_err):
    """断言某个 API 的返回值与 last-error 哨兵。返回失败数。"""
    ret = s.wait_result(NAME, key[0], timeout=15.0)
    err = s.wait_result(NAME, key[1], timeout=15.0)
    fails = 0
    if ret is None or err is None:
        print("  [FAIL] {}: 无结果".format(key[0]))
        return 1
    ret, err = int(ret), int(err)
    if ret != expect_ret:
        print("  [FAIL] {} 返回值 = {}（期望 {}）".format(key[0], ret, expect_ret))
        fails += 1
    else:
        print("  [PASS] {} 返回值 = {}（WAIT_TIMEOUT）".format(key[0], ret))
    if err != expect_err:
        print("  [FAIL] {} 后 last-error = {}（期望哨兵 {}）—— Detour 污染了调用方状态".format(
            key[0], err, expect_err))
        fails += 1
    else:
        print("  [PASS] {} 后 last-error 保持哨兵 {}（Detour 对调用方透明）".format(
            key[0], expect_err))
    return fails


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            print("  [INFO] 注入目标 cmd PID={}".format(s.target_pid))
            s.run_target(NAME, TARGET_BODY, ready_key="READY")

            hev = s.wait_result(NAME, "HEVENT_OK", timeout=10.0)
            if hev != "1":
                print("  [FAIL] CreateEventW 失败，用例前提不成立")
                return failures + 1

            failures += _check(s, ("WFSO_RET", "WFSO_ERR"),
                               expect_ret=0x102, expect_err=SENTINEL)
            failures += _check(s, ("WFMO_RET", "WFMO_ERR"),
                               expect_ret=0x102, expect_err=SENTINEL)

            if not s.wait_done(NAME, timeout=10.0):
                print("  [FAIL] 目标脚本未 done()")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
