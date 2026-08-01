"""特性: Ctrl+Z → EOF（line input 模式）    类别: keyboard

链路: SendInput(Ctrl+Z) → WT → mediator → DLL → 目标 ReadConsoleW 读到 EOF

预期:
  - 目标脚本 SetConsoleMode(LINE_INPUT|PROCESSED_INPUT) 后 ReadConsoleW
  - Ctrl+Z 使 ReadConsoleW 返回 TRUE 且读取数=0（EOF 语义）
  - READ_RET 断言（BUG-004 已修复：LineEditor ProcessKey 新增 Ctrl+Z 分支，
    空行时截断后返回空行 → ReadConsoleW 返回 TRUE *read=0；
    行非空时从光标处截断并提交截断行）

验证方式: 目标脚本自检（READ_RET=<ok> <n>）
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from helpers import input_sim

NAME = "ctrl_z_eof"

TARGET_BODY = '''
rec("READY", "PASS")
h_in = get_std_in()
set_mode(h_in, ENABLE_LINE_INPUT | ENABLE_PROCESSED_INPUT)
_k.FlushConsoleInputBuffer(h_in)
buf = ctypes.create_unicode_buffer(64)
n = wintypes.DWORD(0)
ok = _k.ReadConsoleW(h_in, buf, 63, ctypes.byref(n), None)
rec("READ_RET", str(int(ok)) + " " + str(n.value))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(1.0)
            input_sim.type_ctrl_z()
            v = s.wait_result(NAME, "READ_RET", timeout=8.0)
            if v:
                ok_s, n_s = v.split()
                ok_pass = (ok_s == "1")
                n_pass = (n_s == "0")
                if ok_pass and n_pass:
                    print("  [PASS] READ_RET ok=1 n=0 (EOF 语义)")
                else:
                    print("  [FAIL] READ_RET ok={} n={} (期望 ok=1 n=0)".format(
                        ok_s, n_s))
                    failures += 1
            else:
                print("  [FAIL] READ_RET: 无结果（超时，Ctrl+Z 未触发 EOF）")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
