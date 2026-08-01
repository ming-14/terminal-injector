"""特性: 制表符（HT/BS/TBC/HTS）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - HT 0x09、BS 0x08、TBC g 清制表位、HTS H 设制表位 共 4 个序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\t", "LOG_HT"),          # 横向制表
    (b"\x08", "LOG_BS"),        # 退格
    (b"\x1b[g", "LOG_TBC"),     # 清除制表位
    (b"\x1bH", "LOG_HTS"),      # 设置制表位
]


def run() -> int:
    return run_vt_byte_test("tabs", SEQS)


if __name__ == "__main__":
    sys.exit(run())
