"""特性: 光标归位与清除前置（\x1b[H 与 \x1b[2J 组合，典型全屏重绘开头）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - 组合序列 "\x1b[2J\x1b[H" 原样到达日志（覆盖 HVP 无参形式与合并批次）
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[2J\x1b[H", "LOG_CLEAR_HOME"),  # 清屏并归位（单次合并写入）
    (b"\x1b[H", "LOG_HOME_ALONE"),         # 单独 HVP 归位
]


def run() -> int:
    return run_vt_byte_test("clear_home", SEQS)


if __name__ == "__main__":
    sys.exit(run())
