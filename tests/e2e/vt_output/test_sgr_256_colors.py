"""特性: SGR 256 色（38;5;n 前景 / 48;5;n 背景）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - 抽样覆盖边界值（0/16/17/255）与中间值（196）的 5 个序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[38;5;0m", "LOG_256_FG_0"),        # 0 = 黑色边界
    (b"\x1b[38;5;16m", "LOG_256_FG_16"),      # 16 = 16 色区末 + 256 区起点
    (b"\x1b[38;5;17m", "LOG_256_FG_17"),      # 256 区第二色
    (b"\x1b[38;5;255m", "LOG_256_FG_255"),    # 255 = 最大值边界
    (b"\x1b[48;5;196m", "LOG_256_BG_196"),    # 背景 256 色
]


def run() -> int:
    return run_vt_byte_test("sgr_256_colors", SEQS)


if __name__ == "__main__":
    sys.exit(run())
