"""特性: WriteConsoleA 写入 ANSI（GBK 代码页）    类别: console_api

链路: 目标程序 SetConsoleOutputCP(936) → WriteConsoleA(GBK 字节)
      → DLL 转换翻译 → mediator → WT

预期:
  - 写 "你好" 的 GBK 编码（4 字节），返回 TRUE 且写入数=2（WriteConsoleA 按字符数计）
  - 光标推进 4 列（2 字符 × 2 列）
  - 无乱码：翻译字节正确到达（UTF-8 的中文 hex）

验证方式: 目标程序自检（GetConsoleScreenBufferInfo）+ mediator 日志字节
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "write_console_ansi"

TARGET_BODY = '''
rec("READY", "PASS")
ok_cp = _k.SetConsoleOutputCP(936)
check("SET_CP", bool(ok_cp), "SetConsoleOutputCP err={}".format(ctypes.get_last_error()))
h_out = get_std_out()
info = get_csbi(h_out)
if info is None:
    check("CSBI", False, "GetConsoleScreenBufferInfo failed")
    done()
    sys.exit(1)
row = info.dwCursorPosition.Y + 1
_k.SetConsoleCursorPosition(h_out, COORD(0, row))
buf = "\\u4f60\\u597d".encode("gbk")   # "你好" GBK = 4 字节
n = wintypes.DWORD(0)
ok = _k.WriteConsoleA(h_out, buf, len(buf), ctypes.byref(n), None)
check("WRITE_ANSI_RET", bool(ok) and n.value == 2, "ok={} n={}".format(ok, n.value))
check("CURSOR_ANSI", cursor_pos(h_out) == (4, row),
      "expected (4,{}) got {}".format(row, cursor_pos(h_out)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("SET_CP", "WRITE_ANSI_RET", "CURSOR_ANSI"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
