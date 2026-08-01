"""特性: SGR 真彩色 24-bit（38;2;r;g;b / 48;2;r;g;b）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - 覆盖黑 (0,0,0)、白 (255,255,255)、任意色 (12,34,56) 与背景真彩的 4 个
    序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[38;2;0;0;0m", "LOG_TRUE_FG_BLACK"),        # 黑
    (b"\x1b[38;2;255;255;255m", "LOG_TRUE_FG_WHITE"),  # 白 = 上边界
    (b"\x1b[38;2;12;34;56m", "LOG_TRUE_FG_ANY"),       # 任意色
    (b"\x1b[48;2;200;100;50m", "LOG_TRUE_BG"),         # 背景真彩
]


def run() -> int:
    return run_vt_byte_test("sgr_truecolor", SEQS)


if __name__ == "__main__":
    sys.exit(run())
