"""特性: 自动换行与换行模式（?7h/l、LNM 20h/l、CR）    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL → mediator → WT

预期:
  - ?7h 开自动换行、?7l 关、20h 开 LNM（LF 转 CRLF）、20l 关、CR 回车
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
    (b"\x1b[?7h", "LOG_WRAP_ON"),    # 开启自动换行
    (b"\x1b[?7l", "LOG_WRAP_OFF"),   # 关闭自动换行
    (b"\x1b[20h", "LOG_LNM_ON"),     # 开启换行模式（LF→CRLF）
    (b"\x1b[20l", "LOG_LNM_OFF"),    # 关闭换行模式
    (b"\r", "LOG_CR"),               # 回车
]


def run() -> int:
    return run_vt_byte_test("line_wrap", SEQS)


if __name__ == "__main__":
    sys.exit(run())
