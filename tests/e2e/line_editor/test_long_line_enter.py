"""特性: 长命令软换行后回车的光标定位    类别: line_editor

回归测试（2026-08-02 修复 SyncCursor 软换行计算）:
  长命令显示宽度超过屏幕宽度时 WT 软换行成多行，回车后 LineEditor 的
  SyncCursor 必须把折行行数计入 Y，否则 ConsoleState 光标少一行 →
  cmd 后续输出（WriteConsoleW 前发的 CursorPosition，OutputHooks.cpp:138）
  定位错行 → 新 prompt 覆盖命令续行（用户报告：回车后光标停在续行开头）

链路: type_text 超长命令 → Enter → python 目标进程的 ReadConsoleW 走 DLL
      LineEditor（行编辑模式）→ SyncCursor 更新 python 进程 ConsoleState →
      python 退出 → mediator ChildExitSync 上报 DLL 光标

预期（日志断言）:
  ChildExitSync sent cursor=(X,Y) 的 Y = 基线.Y + 1 + wrap
    wrap = (基线.X + 行宽 - 1) / 屏幕宽度W   （行宽 = 输入字符数）
  X = 0
  基线 = python LazyInit aligned 光标（"child cursor aligned to WT"），
  即 SyncCursor 的起始 ConsoleState 光标（python 启动时 WT 真实位置）。
  （不可用目标自检 START：目标 GetConsoleScreenBufferInfo 返回
    VirtualConsoleState（Phase 14），与 ConsoleState 存在时序差——
    偶发读到滞后值导致期望偏差。故基线取 DLL 日志 aligned 值。）

验证方式: 目标 ReadConsoleW 读一行 + rec 起始状态；驱动解析 ChildExitSync
"""
import glob
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from common import childlog

NAME = "long_line_enter"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
info0 = get_csbi(get_std_out())
rec("W", str(info0.dwSize.X))
sx, sy = cursor_pos()
rec("START", "%d,%d" % (sx, sy))
# ReadConsoleW 读一行（长命令，Enter 后返回；含尾部 \\r\\n）
buf = ctypes.create_unicode_buffer(4096)
n = wintypes.DWORD(0)
ok = _k.ReadConsoleW(get_std_in(), buf, 4095, ctypes.byref(n), None)
line = buf[:n.value]
rec("LINE_LEN", str(len(line)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            v_w = s.wait_result(NAME, "W", timeout=20.0)
            v_st = s.wait_result(NAME, "START", timeout=10.0)
            if not v_w or not v_st:
                print("  [FAIL] W/START: 无结果")
                failures += 1
            else:
                W = int(v_w)
                sx, sy = (int(x) for x in v_st.split(","))
                baseline = childlog.find_child_aligned_baseline()
                if baseline is None:
                    print("  [FAIL] 未从 python DLL 日志解析到 aligned 基线")
                    failures += 1
                    return failures
                ax, ay = baseline
                n_chars = W + 30  # 保证折行
                s.type_text("a" * n_chars)
                time.sleep(0.3)
                s.type_enter()

                v_len = s.wait_result(NAME, "LINE_LEN", timeout=20.0)
                if not v_len:
                    print("  [FAIL] LINE_LEN: 无结果")
                    failures += 1
                else:
                    line_len = int(v_len)
                    if line_len != n_chars + 2:
                        print("  [FAIL] LINE_LEN={}（期望 {}：行 + 尾部 \\r\\n）".format(
                            line_len, n_chars + 2))
                        failures += 1
                    else:
                        print("  [PASS] 回车后 ReadConsoleW 返回完整行 ({} 字符 + \\r\\n)".format(
                            n_chars))

                    # 等 python 退出后 ChildExitSync 上报光标
                    v_done = s.wait_result(NAME, "DONE", timeout=15.0)
                    time.sleep(0.5)
                    log_text = s.log().read_all()
                    m = re.search(r"ChildExitSync sent cursor=\((\d+),(\d+)\)",
                                  log_text)
                    if not m:
                        print("  [FAIL] 日志未找到 ChildExitSync 光标上报")
                        failures += 1
                    else:
                        ex, ey = int(m.group(1)), int(m.group(2))
                        wrap = (ax + n_chars - 1) // W
                        exp_y = ay + 1 + wrap
                        print("  [INFO] W={} aligned=({},{}) START=({},{}) 折行数={} "
                              "期望光标=(0,{}) ChildExitSync=({},{})".format(
                                  W, ax, ay, sx, sy, wrap, exp_y, ex, ey))
                        if ex == 0 and ey == exp_y:
                            print("  [PASS] 回车后光标 (0,{}) 含折行数 {}（修复生效）".format(
                                ey, wrap))
                        else:
                            print("  [FAIL] 光标 ({},{})（期望 (0,{})——SyncCursor 未计入"
                                  "软换行行数，回车后新 prompt 将覆盖续行）".format(ex, ey, exp_y))
                            failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
