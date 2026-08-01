"""特性: SGR 亮色（前景 90-97 / 背景 100-107）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - 前景 90-97、背景 100-107 共 16 个序列全部原样到达 mediator 日志
  - VT 直通模式不维护虚拟光标状态（程序自维护），故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

# (VT 字节, 断言 KEY)：亮色前景 90-97 与背景 100-107
SEQS = [
    (b"\x1b[90m", "LOG_BRIGHT_FG_90"),
    (b"\x1b[91m", "LOG_BRIGHT_FG_91"),
    (b"\x1b[97m", "LOG_BRIGHT_FG_97"),
    (b"\x1b[100m", "LOG_BRIGHT_BG_100"),
    (b"\x1b[101m", "LOG_BRIGHT_BG_101"),
    (b"\x1b[107m", "LOG_BRIGHT_BG_107"),
]


def run() -> int:
    return run_vt_byte_test("sgr_bright_colors", SEQS)


if __name__ == "__main__":
    sys.exit(run())
