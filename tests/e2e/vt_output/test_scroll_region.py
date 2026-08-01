"""特性: 滚动（SU/SD）与滚动区域（DECSTBM）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - DECSTBM 3;10r 设滚动区、SU S 上滚、SD T 下滚 共 3 个序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[3;10r", "LOG_DECSTBM"),  # 滚动区 3-10 行
    (b"\x1b[2S", "LOG_SU"),          # 上滚 2 行
    (b"\x1b[2T", "LOG_SD"),          # 下滚 2 行
]


def run() -> int:
    return run_vt_byte_test("scroll_region", SEQS)


if __name__ == "__main__":
    sys.exit(run())
