"""特性: 方向键命令历史导航    类别: line_editor

链路: SendInput(type_text/type_arrow) → WT → mediator → DLL LineEditor 历史 → 执行

预期:
  - 执行两条命令进入历史（echo one / echo two）
  - 输入新命令不回车，Up 切换到上一条、再 Up 再上一条、Down 回到下一条
  - Enter 执行的是历史导航选中的命令（结果文件验证）

验证方式: cmd 重定向结果文件内容（导航选中项被执行）
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import paths
from common import result as result_mod

NAME = "history_nav"
R1 = os.path.join(paths.RESULTS_DIR, NAME + "_1.txt")
R2 = os.path.join(paths.RESULTS_DIR, NAME + "_2.txt")
R3 = os.path.join(paths.RESULTS_DIR, NAME + "_3.txt")


def wait_file_content(path: str, timeout: float = 10.0) -> str:
    """等待文件出现并返回 strip 后内容；超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read().strip()
            except OSError:
                pass
        time.sleep(0.2)
    return None


def run() -> int:
    result_mod.clear_result(NAME)
    for p in (R1, R2, R3):
        try:
            os.remove(p)
        except OSError:
            pass
    failures = 0
    try:
        with TestSession() as s:
            time.sleep(0.5)

            # 1. 两条命令进历史
            s.type_text('echo one > "{}"'.format(R1))
            s.type_enter()
            if wait_file_content(R1, timeout=15.0) != "one":
                print("  [FAIL] CMD1: echo one 未执行成功")
                failures += 1
            else:
                print("  [PASS] CMD1 历史第一条执行")
            s.type_text('echo two > "{}"'.format(R2))
            s.type_enter()
            if wait_file_content(R2, timeout=15.0) != "two":
                print("  [FAIL] CMD2: echo two 未执行成功")
                failures += 1
            else:
                print("  [PASS] CMD2 历史第二条执行")
            time.sleep(0.5)

            # 2. 输入新命令（不回车），Up/Up/Down 导航历史
            s.type_text("echo three")
            time.sleep(0.5)
            s.type_arrow("up")
            time.sleep(0.5)
            s.type_arrow("up")
            time.sleep(0.5)
            s.type_arrow("down")
            time.sleep(0.5)

            # 3. 追加重定向并回车执行
            s.type_text(' > "{}"'.format(R3))
            s.type_enter()
            got = wait_file_content(R3, timeout=15.0)
            if got == "two":
                print("  [PASS] NAV 历史导航选中 echo two 并执行")
            else:
                print("  [FAIL] NAV: 执行结果={!r}（期望 two，导航未生效）".format(got))
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
