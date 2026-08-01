"""特性: 屏幕反色（?5h/l，DECSCNM）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - ?5h 开启反色、?5l 关闭 共 2 个序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[?5h", "LOG_REVERSE_ON"),   # 开启反色
    (b"\x1b[?5l", "LOG_REVERSE_OFF"),  # 关闭反色
]


def run() -> int:
    return run_vt_byte_test("reverse_video", SEQS)


if __name__ == "__main__":
    sys.exit(run())
