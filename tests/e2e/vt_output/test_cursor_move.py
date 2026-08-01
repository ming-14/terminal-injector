"""特性: 光标移动（CUP/CUU/CUD/CUF/CUB/CHA/VPA/CNL/CPL）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - CUP r;cH、CUU A、CUD B、CUF C、CUB D、CHA G、VPA d、CNL E、CPL F
    共 9 个序列全部原样到达日志
  - VT 直通模式不维护虚拟光标状态（程序自维护，Phase 13 设计），
    光标位置断言由 console_api 类别（WriteConsoleW 翻译链 + 虚拟状态）覆盖
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[10;20H", "LOG_CUP"),     # CUP 行;列
    (b"\x1b[5A", "LOG_CUU"),         # CUU 上移 5
    (b"\x1b[5B", "LOG_CUD"),         # CUD 下移 5
    (b"\x1b[5C", "LOG_CUF"),         # CUF 右移 5
    (b"\x1b[5D", "LOG_CUB"),         # CUB 左移 5
    (b"\x1b[10G", "LOG_CHA"),        # CHA 列 10
    (b"\x1b[10d", "LOG_VPA"),        # VPA 行 10
    (b"\x1b[3E", "LOG_CNL"),         # CNL 下移 3 行
    (b"\x1b[3F", "LOG_CPL"),         # CPL 上移 3 行
]


def run() -> int:
    return run_vt_byte_test("cursor_move", SEQS)


if __name__ == "__main__":
    sys.exit(run())
