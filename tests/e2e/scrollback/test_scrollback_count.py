"""特性: 回滚行数跟踪    类别: scrollback

链路: cmd 输出满宽行（echo 120 字符）→ cmd DLL AdvanceCursor 滚动 →
      scrollback 递增 → 缩小 WT 窗口 → WtSizeWatcher → cmd DLL
      ApplyWtResize → 日志记录 scrollback（Phase 18: 不因 resize 丢失）
      子进程（python）段：输出超屏 → resize → 子进程 DLL 也 ApplyWtResize
      （修复 2 验证：ResizeNotify 同步 VirtualConsoleState）

预期（Phase 18 语义）:
  - 输出 40 行满宽后 scrollback > 0（滚动已发生）
  - 再输出 50 行后 scrollback 单调增（s2 > s1）
  - 不输出再次 resize：scrollback 保持不变（s3 == s2，保留语义）
  - 子进程 python 输出超屏 + resize 后，其 DLL 日志 ApplyWtResize
    scrollback > 0（修复前子进程只收 ResizeNotify 不调 ApplyWtResize）

说明: 绝对值受 cmd 命令回显/折行影响不可精确预测，用单调性 + 保留断言。
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from common.childlog import latest_injected_log
from helpers import input_sim

NAME = "scrollback_count"

CHILD_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
import ctypes, os as _os
k32 = ctypes.windll.kernel32
k32.WriteConsoleW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                              ctypes.c_uint, ctypes.POINTER(ctypes.c_ulong),
                              ctypes.c_void_p]
k32.WriteConsoleW.restype = ctypes.c_int
h_out = get_std_out()
# 输出 R+3 行满宽（R 由注入后屏幕行数决定：用 120x30 视口），滚动 3 行
text = (("A" * 120) + "\\r\\n") * 33
wbuf = ctypes.create_unicode_buffer(text)
written = ctypes.c_ulong(0)
k32.WriteConsoleW(h_out, wbuf, len(wbuf.value.encode("utf-16-le")) // 2,
                  ctypes.byref(written), None)
rec("WRITTEN", str(int(written.value)))
time.sleep(4.0)  # 驱动 resize WT 窗口的窗口期
rec("PID", str(_os.getpid()))
done()
'''


def _read_scb(pid: int) -> tuple:
    """读 cmd 进程 DLL 日志的最新 scrollback/userBufH（ApplyWtResize 行）。"""
    p = latest_injected_log(pid)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8", errors="ignore") as f:
        c = f.read()
    m = re.findall(r"ApplyWtResize: [^\n]*scrollback=(\d+) userBufH=(\d+)", c)
    if not m:
        return None
    return int(m[-1][0]), int(m[-1][1])


def _resize_wt(delta_px: int) -> None:
    import win32gui
    from helpers import injector
    hwnd = injector._test_wt_hwnd or injector.find_wt_windows()[-1]
    rect = win32gui.GetWindowRect(hwnd)
    win32gui.SetWindowPos(hwnd, None, rect[0], rect[1],
                          rect[2] - rect[0], rect[3] - rect[1] - delta_px,
                          0x0040)  # SWP_NOZORDER


def run() -> int:
    failures = 0
    try:
        with TestSession() as s:
            pid = s.target_pid
            line120 = "A" * 120
            # 1) 输出 40 行满宽
            input_sim.type_text("for /L %%i in (1,1,40) do echo %s" % line120)
            input_sim.press_key(input_sim.VK_RETURN)
            time.sleep(3.0)
            _resize_wt(120)
            time.sleep(2.5)
            s1 = _read_scb(pid)
            # 2) 再输出 50 行满宽
            input_sim.type_text("for /L %%i in (1,1,50) do echo %s" % line120)
            input_sim.press_key(input_sim.VK_RETURN)
            time.sleep(3.0)
            _resize_wt(120)
            time.sleep(2.5)
            s2 = _read_scb(pid)
            # 3) 不输出，再次 resize：scrollback 应保留
            _resize_wt(60)
            time.sleep(2.5)
            s3 = _read_scb(pid)

            print("  [INFO] scrollback: 40行后={} 50行后={} 再resize={}".format(
                s1, s2, s3))
            if s1 is None or s2 is None or s3 is None:
                print("  [FAIL] 未读取到 ApplyWtResize scrollback (pid={})".format(pid))
                failures += 1
            else:
                if s1[0] > 0:
                    print("  [PASS] 输出 40 行满宽后 scrollback={}（滚动已发生）".format(s1[0]))
                else:
                    print("  [FAIL] scrollback={}（期望 > 0）".format(s1[0]))
                    failures += 1
                if s2[0] > s1[0]:
                    print("  [PASS] 再输出 50 行后 scrollback {} -> {}（单调增）".format(
                        s1[0], s2[0]))
                else:
                    print("  [FAIL] scrollback 未单调增: {} -> {}".format(s1[0], s2[0]))
                    failures += 1
                if s3[0] == s2[0]:
                    print("  [PASS] 无输出 resize 后 scrollback 保留 {}（Phase 18 语义）".format(
                        s3[0]))
                else:
                    print("  [FAIL] scrollback 变化: {} -> {}（期望保留 {}）".format(
                        s2[0], s3[0], s2[0]))
                    failures += 1
                if s3[1] == 0:
                    print("  [PASS] userBufH=0（未调 SetConsoleScreenBufferSize）")
                else:
                    print("  [FAIL] userBufH={}（期望 0）".format(s3[1]))
                    failures += 1
            # 子进程段：修复 2 验证（子进程 ResizeNotify → ApplyWtResize）
            result_mod.clear_result(NAME + "_child")
            s.run_target(NAME + "_child", CHILD_BODY, ready_key="READY")
            vc = s.wait_result(NAME + "_child", "WRITTEN", timeout=15.0)
            if not vc:
                print("  [FAIL] 子进程段: WRITTEN 无结果")
                failures += 1
            else:
                _resize_wt(120)
                vp = s.wait_result(NAME + "_child", "PID", timeout=15.0)
                if not vp:
                    print("  [FAIL] 子进程段: PID 无结果")
                    failures += 1
                else:
                    cpid = vp.strip()
                    log_path = latest_injected_log(int(cpid))
                    deadline = time.time() + 6.0
                    m = None
                    while time.time() < deadline:
                        if os.path.exists(log_path):
                            with open(log_path, "r", encoding="utf-8",
                                      errors="ignore") as f:
                                cc = f.read()
                            ms = re.findall(
                                r"ApplyWtResize: [^\n]*scrollback=(\d+)", cc)
                            if ms:
                                m = ms
                                break
                        time.sleep(0.3)
                    if not m:
                        print("  [FAIL] 子进程 injected_{}.log 无 ApplyWtResize".format(cpid))
                        failures += 1
                    else:
                        scb = int(m[-1])
                        print("  [INFO] 子进程 ApplyWtResize scrollback={}".format(scb))
                        if scb > 0:
                            print("  [PASS] 子进程 scrollback={}（ResizeNotify 同步生效）".format(scb))
                        else:
                            print("  [FAIL] 子进程 scrollback={}（期望 > 0）".format(scb))
                            failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
