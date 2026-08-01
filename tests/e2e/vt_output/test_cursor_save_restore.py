"""特性: 光标保存/恢复（DECSC/DECRC、CSI s/u）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - DECSC（ESC 7）、DECRC（ESC 8）、CSI s、CSI u 共 4 个序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b7", "LOG_DECSC"),     # DECSC 保存光标
    (b"\x1b8", "LOG_DECRC"),     # DECRC 恢复光标
    (b"\x1b[s", "LOG_CSI_SAVE"), # CSI s 保存光标
    (b"\x1b[u", "LOG_CSI_RESTORE"),  # CSI u 恢复光标
]


def run() -> int:
    return run_vt_byte_test("cursor_save_restore", SEQS)


if __name__ == "__main__":
    sys.exit(run())
