"""特性: 插入/删除（ICH/DCH/ECH 字符、IL/DL 行）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - ICH @ 插字符、DCH P 删字符、ECH X 擦字符、IL L 插行、DL M 删行
    共 5 个序列原样到达日志
  - VT 直通模式不维护虚拟光标状态，故仅字节验证
  - 结果文件 SET_VT_MODE=PASS

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    (b"\x1b[2@", "LOG_ICH"),  # 插入 2 字符
    (b"\x1b[2P", "LOG_DCH"),  # 删除 2 字符
    (b"\x1b[2X", "LOG_ECH"),  # 擦除 2 字符
    (b"\x1b[2L", "LOG_IL"),   # 插入 2 行
    (b"\x1b[2M", "LOG_DL"),   # 删除 2 行
]


def run() -> int:
    return run_vt_byte_test("insert_delete_chars", SEQS)


if __name__ == "__main__":
    sys.exit(run())
