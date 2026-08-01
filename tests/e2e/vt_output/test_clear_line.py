"""特性: 清行（EL 0/1/2）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - EL 0 光标右、1 光标左、2 整行 共 3 个序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[0K", "LOG_EL_0"),  # 清除光标右方
    (b"\x1b[1K", "LOG_EL_1"),  # 清除光标左方
    (b"\x1b[2K", "LOG_EL_2"),  # 清除整行
]


def run() -> int:
    return run_vt_byte_test("clear_line", SEQS)


if __name__ == "__main__":
    sys.exit(run())
