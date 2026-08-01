"""特性: 光标显隐与闪烁（?25h/l、?12h/l）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - ?25l 隐藏、?25h 显示、?12l 关闪烁、?12h 开闪烁 共 4 个序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[?25l", "LOG_CURSOR_HIDE"),    # 隐藏光标
    (b"\x1b[?25h", "LOG_CURSOR_SHOW"),    # 显示光标
    (b"\x1b[?12l", "LOG_CURSOR_NOBLINK"), # 关闭闪烁
    (b"\x1b[?12h", "LOG_CURSOR_BLINK"),   # 开启闪烁
]


def run() -> int:
    return run_vt_byte_test("cursor_visibility", SEQS)


if __name__ == "__main__":
    sys.exit(run())
