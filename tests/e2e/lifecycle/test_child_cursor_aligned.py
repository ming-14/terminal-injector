"""特性: 子进程注入后的光标基准（HelloAck 对齐）    类别: lifecycle

回归测试（2026-08-02 修复 长命令回车后光标跳回折行处）:
  cmd 交互中输入长命令（软折行）回车后，python 子进程输出会把 WT 光标
  拉回 ConHost 旧位置（如折行行首）——用户反馈"回车后光标又跳回去"。

根因（LazyInit.cpp 屏幕重放分支）:
  子进程 LazyInit 时 ConHost 快照 = cmd 注入时刻的陈旧内容/光标：
  - 重放分支把 ConHost 陈旧内容重新发给 WT，覆盖当前正确屏幕
  - 并把 WT 光标同步到 ConHost 快照光标（如 (0,4)），而非 WT 真实位置
  - python 输出前 WriteConsoleW cursorSync 又发一次 CursorPosition → 光标被拉回

修复:
  仅主目标进程（isTarget=1，cmd）执行屏幕重放；
  子进程（isTarget=0）跳过重放，用 HelloAck 回传的 WT 真实光标
  对齐缓存（ChildSession Handshake 注释即此意图，此前被重放分支覆盖）。

断言:
  - 子进程 DLL 日志出现 "child cursor aligned to WT (X,Y) from HelloAck"
  - 子进程 DLL 日志不出现 "WT cursor synced to terminal"（重放分支不执行）

验证方式: 驱动在注入 cmd 中执行长命令（生成 python 子进程），读 C:\\temp\\injected_<pid>.log
"""
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "child_cursor_aligned"

TARGET_BODY = '''
rec("READY", "PASS")
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            # 在 cmd 中执行一条超长命令（折行），生成 python 子进程
            long_cmd = ('python "C:\\Users\\rikka\\Desktop\\e2e\\_targets\\'
                        'exp_mouse_move.py" "C:\\Users\\rikka\\Desktop\\e2e\\'
                        'results\\exp_mouse_move.txt"')
            s.type_text(long_cmd)
            time.sleep(0.3)
            s.type_enter()
            time.sleep(3.5)

            # 找最新创建的子进程 DLL 日志（LazyInit aligned 记录）
            logs = sorted(glob.glob(r"C:\temp\injected_*.log"),
                          key=os.path.getmtime)
            if not logs:
                print("  [FAIL] 未找到任何 injected_*.log")
                failures += 1
                return failures

            target_log = None
            for lp in reversed(logs):
                try:
                    with open(lp, "r", encoding="utf-8", errors="replace") as f:
                        content = f.read()
                except OSError:
                    continue
                if "child cursor aligned to WT" in content:
                    target_log = (lp, content)
                    break
            if target_log is None:
                print("  [FAIL] 子进程 DLL 日志未见 child cursor aligned 记录")
                failures += 1
                return failures

            lp, content = target_log
            import re
            m = re.search(r"child cursor aligned to WT \((\d+),(\d+)\) from HelloAck",
                          content)
            if not m:
                print("  [FAIL] 日志格式不符: {}".format(lp))
                failures += 1
                return failures
            cx, cy = int(m.group(1)), int(m.group(2))
            print("  [INFO] {}: aligned to WT ({},{})".format(lp, cx, cy))

            if "WT cursor synced to terminal" in content:
                print("  [FAIL] 子进程仍执行了屏幕重放分支（WT cursor synced）——"
                      "修复未生效")
                failures += 1
            else:
                print("  [PASS] 子进程跳过屏幕重放（无 WT cursor synced 记录）")

            if cx > 0 or cy > 0:
                print("  [PASS] 子进程光标对齐到 HelloAck 的 WT 真实光标 ({},{})".format(
                    cx, cy))
            else:
                print("  [FAIL] aligned 光标为 (0,0)（HelloAck 未携带有效 WT 光标）")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
