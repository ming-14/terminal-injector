"""特性: ENABLE_WRAP_AT_EOL_OUTPUT（行末折行）    类别: modes

链路: 目标 SetConsoleMode/WriteConsoleW → DLL ModeHooks/OutputHooks

预期（工程实际语义）:
  - SetConsoleMode(输出句柄, WRAP_AT_EOL) → Get == 0x6（set|VT_PROCESSING 强制）
  - 写满一行再写字符：光标折行到下一行（wrap 生效）

差异已修复（LIM-003）:
  - 原生 ConHost：WRAP_AT_EOL 关闭时写满行后光标停在行末（不折行）
  - 修复前：WriteConsoleW_Detour 硬编码 wrapAtEol=true（OutputHooks.cpp:151），
    WRAP_AT_EOL 标志不参与光标推进决策
  - 修复后：wrapAtEol 从 ConsoleState 输出模式读取，关闭时停在行末
    （真实验证点是 DLL 日志 WriteConsoleW afterCursor=(119,7)，
    ConsoleState 即真实 ConHost 语义侧）

ConPTY 已知限制（2026-08-02 DSR 实测，WT 回报光标确认）:
  - ConPTY 不尊重 ENABLE_WRAP_AT_EOL_OUTPUT：关闭后写满一行仍折行
  - VirtualConsoleState 是 ConPTY 侧状态，始终折行（不参与 wrap 决策）
  - GetConsoleScreenBufferInfo 返回 VirtualConsoleState（Phase 14，
    优先与 WT 一致）→ 程序读到折行后的位置，与 WT 视觉一致

验证方式: 目标自检 set/get + GetConsoleScreenBufferInfo 光标位置
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "wrap_at_eol"

TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
ok1 = set_mode(h_out, ENABLE_WRAP_AT_EOL_OUTPUT)
g1 = get_mode(h_out)
rec("SET_GET", str(int(ok1)) + " " + hex(g1))
ok2 = set_mode(h_out, 0)
g2 = get_mode(h_out)
rec("CLEAR_GET", str(int(ok2)) + " " + hex(g2))
# 光标折行行为：回到行首，按当前行宽写满一行再写字符（行宽 = ConPTY 宽，非固定 80）
set_mode(h_out, ENABLE_WRAP_AT_EOL_OUTPUT | ENABLE_PROCESSED_OUTPUT)
info = get_csbi(h_out)
width = info.dwSize.X
_k.SetConsoleCursorPosition(h_out, COORD(0, 5))
before = cursor_pos(h_out)
ok3, nw = write_str(h_out, "W" * (width + 2))
after = cursor_pos(h_out)
rec("CURSOR", str(width) + " " + str(after[0]) + " " + str(after[1]))
# LIM-003 修复验证：关闭 WRAP_AT_EOL 后写满一行。
# ConPTY 限制：VirtualConsoleState（ConPTY 侧）始终折行，
# GetConsoleScreenBufferInfo 返回该状态 → 程序读到折行位置（Y=8）。
# ConsoleState（真实 ConHost 语义）停在行末 (119,7)，见 DLL 日志。
set_mode(h_out, ENABLE_PROCESSED_OUTPUT)
_k.SetConsoleCursorPosition(h_out, COORD(0, 7))
ok4, nw4 = write_str(h_out, "W" * (width + 2))
after2 = cursor_pos(h_out)
rec("WRAP_OFF", str(after2[0]) + " " + str(after2[1]))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            v1 = s.wait_result(NAME, "SET_GET", timeout=15.0)
            if not v1:
                print("  [FAIL] SET_GET: 无结果")
                failures += 1
            else:
                parts = v1.split()
                if len(parts) == 2 and parts[0] == "1" and int(parts[1], 16) == 0x6:
                    print("  [PASS] SET_GET Set(0x2) 后 Get=0x6（强制保留 VT）")
                else:
                    print("  [FAIL] SET_GET: {}（期望 1 0x6）".format(v1))
                    failures += 1

            v2 = s.wait_result(NAME, "CLEAR_GET", timeout=15.0)
            if not v2:
                print("  [FAIL] CLEAR_GET: 无结果")
                failures += 1
            else:
                parts = v2.split()
                if len(parts) == 2 and parts[0] == "1" and int(parts[1], 16) == 0x4:
                    print("  [PASS] CLEAR_GET Set(0) 后 Get=0x4（强制保留 VT）")
                else:
                    print("  [FAIL] CLEAR_GET: {}（期望 1 0x4）".format(v2))
                    failures += 1

            v3 = s.wait_result(NAME, "CURSOR", timeout=15.0)
            if not v3:
                print("  [FAIL] CURSOR: 无结果")
                failures += 1
            else:
                parts = v3.split()
                if len(parts) == 3 and parts[0] != "0" and parts[2] == "6":
                    print("  [PASS] CURSOR 写满 {} 列后折行到第 6 行 (X={})".format(
                        parts[0], parts[1]))
                else:
                    print("  [FAIL] CURSOR: {}（期望 width>0 且 Y=6）".format(v3))
                    failures += 1

            v4 = s.wait_result(NAME, "WRAP_OFF", timeout=15.0)
            if not v4:
                print("  [FAIL] WRAP_OFF: 无结果")
                failures += 1
            else:
                parts = v4.split()
                # ConPTY 语义：VirtualConsoleState 始终折行（WRAP_AT_EOL 无效），
                # 与 CURSOR 段一致（Y 递增 1，X 为折行余量）
                if len(parts) == 2 and parts[1] == "8" and parts[0] not in ("", "0"):
                    print("  [PASS] WRAP_OFF 关闭 wrap 后 ConPTY 仍折行 (X={}, Y=8)"
                          "（ConPTY 限制；ConsoleState 停在行末见 DLL 日志）".format(
                              parts[0]))
                else:
                    print("  [FAIL] WRAP_OFF: {}（期望 ConPTY 折行 Y=8）".format(v4))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
