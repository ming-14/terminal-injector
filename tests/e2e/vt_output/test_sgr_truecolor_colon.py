"""特性: SGR 真彩色——冒号式写法与空保留位    类别: vt_output

链路: 目标程序 SetConsoleMode(VT输出) → WriteFile 直通 → DLL(VtSgrFilter) → mediator → WT

回归背景（2026-09-25，用户报告 termtest 真彩色渐变错乱）:
  VtSgrFilter 为剥离 ConHost 不支持的删除线（SGR 9/29）会重建 SGR 序列。
  它原本把 ';' 与 ':' 都当分隔符拍平、再一律用 ';' 重建，于是

      \\x1b[38:2::255:0:0m  →  重建为 \\x1b[38;2;;255;0;m
      （空保留位被误当成一个数据通道，真实通道 0 被挤出）

  隔离实测三种形态的解析结果：
      \\x1b[38;2;255;0;0m    → #ff0000  （正确）
      \\x1b[38:2::255:0:0m   → #ff0000  （冒号带空保留位，合法且正确）
      \\x1b[38;2;;255;0m     → default  （被重建出的形态，颜色丢失）

  于是劫持后真彩色渐变条整条反相/丢色，而正常 WT 正常 —— 本项目与 WT 的显示分叉。

预期:
  - 冒号式三种合法写法（38:2:r:g:b、38:2::r:g:b、38:2:0:r:g:b）原样到达日志
  - `38:2::` 的空保留位必须保留，**不得**被重写成带空分号参数的 `38;2;;`
  - 分号式对照（含合法缺省通道）回归不受影响

关于反向断言的用法:
  只断言"冒号式写法不得被转成分号形态"。分号式序列本身（如 `38;2;;255;0`）
  是应用程序自己发的合法输入，不属于"重建产物"，故不在同一用例里混用，
  避免自相矛盾。本文件因此不含任何"合法分号式"序列。

验证方式: mediator 日志 VtOutput/ChildVtOutput 字节（含"不该出现"的反向断言）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.vtbyte import run_vt_byte_test

SEQS = [
    # --- 冒号式（本 bug 主体）：空保留位形态应原样透传 ---
    (b"\x1b[38:2::255:0:0m", "LOG_COLON_FG_RESERVED"),
    (b"\x1b[48:2::255:0:0m", "LOG_COLON_BG_RESERVED"),
    (b"\x1b[38:2::12:34:56m", "LOG_COLON_FG_ANY"),
    (b"\x1b[38:2::10:20:30m", "LOG_COLON_FG_ANY2"),
    # --- 冒号式：无保留位（应用合法写法，原样透传）---
    (b"\x1b[38:2:255:0:0m", "LOG_COLON_FG_PLAIN"),
    # --- 冒号式：显式颜色空间 id ---
    (b"\x1b[38:2:0:255:0:0m", "LOG_COLON_FG_CSPACE"),
    # --- 冒号式 256 色（顺带覆盖）---
    (b"\x1b[38:5:196m", "LOG_COLON_256"),
]


def run() -> int:
    return run_vt_byte_test("sgr_truecolor_colon", SEQS)


if __name__ == "__main__":
    sys.exit(run())
