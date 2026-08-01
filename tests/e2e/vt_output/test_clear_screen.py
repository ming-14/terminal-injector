"""特性: 清屏（ED 0/1/2/3）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - ED 0 光标以下、1 光标以上、2 全屏、3 含滚动缓冲区 共 4 个序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[0J", "LOG_ED_0"),  # 清除光标下方
    (b"\x1b[1J", "LOG_ED_1"),  # 清除光标上方
    (b"\x1b[2J", "LOG_ED_2"),  # 清除全部
    (b"\x1b[3J", "LOG_ED_3"),  # 清除滚动缓冲区
]


def run() -> int:
    return run_vt_byte_test("clear_screen", SEQS)


if __name__ == "__main__":
    sys.exit(run())
