"""特性: WriteFile(stdout) 输出直通（输出模式强制 VT）    类别: console_api

链路: 目标程序 WriteFile(stdout, 字节) → DLL WriteFile Hook → mediator → WT

设计事实（ModeHooks.cpp/OutputHooks.cpp）:
  - SetConsoleMode 对输出句柄强制保留 ENABLE_VIRTUAL_TERMINAL_PROCESSING
    （虚拟状态 outputMode 恒含 VT 标志），GetConsoleMode 同样强制返回 VT
  - WriteFile(stdout) 因此始终走直通翻译路径（cmd 自身输出也依赖此钩子）

预期:
  - SetConsoleMode(stdout, 0x3) 后 GetConsoleMode 仍返回含 VT 标志（强制 VT）
  - VT 模式与"老式模式"下 WriteFile 均返回 TRUE 且字节数正确（数据不丢）
  - 两段写入内容都出现在 mediator 日志（直通翻译，可能合并传输）

验证方式: 目标程序自检 + mediator 日志字节
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "write_file_stdout"

VT_HEX = "76 74 2D 68 65 6C 6C 6F"                  # vt-hello
LEGACY_HEX = "6C 65 67 61 63 79 2D 68 65 6C 6C 6F"  # legacy-hello

TARGET_BODY = '''
rec("READY", "PASS")
h_out = get_std_out()
set_mode(h_out, 0x7)   # 显式开启 VT 处理
ok, n = write_bytes(h_out, b"vt-hello")
check("VT_WRITE_RET", bool(ok) and n == 8, "ok={} n={}".format(ok, n))
set_mode(h_out, 0x3)   # 尝试降级为老式模式（无 VT 标志）
check("MODE_FORCED", get_mode(h_out) == 0x7,
      "mode={}".format(get_mode(h_out)))
ok, n = write_bytes(h_out, b"legacy-hello")
check("LEGACY_WRITE_RET", bool(ok) and n == 12, "ok={} n={}".format(ok, n))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY", ready_timeout=30.0)
            for key in ("VT_WRITE_RET", "MODE_FORCED", "LEGACY_WRITE_RET"):
                v = s.wait_result(NAME, key, timeout=10.0)
                if v == "PASS":
                    print("  [PASS] {}".format(key))
                else:
                    print("  [FAIL] {}: {}".format(key, v or "no result"))
                    failures += 1
            # 轮询等待直通字节落盘（避免读盘时序）
            deadline = time.time() + 10.0
            while time.time() < deadline:
                content = s.log().read_all()
                if VT_HEX in content and LEGACY_HEX in content:
                    break
                time.sleep(0.3)
            if VT_HEX in content and LEGACY_HEX in content:
                print("  [PASS] LOG_DIRECT_BYTES (两段写入内容均到达 WT)")
            else:
                print("  [FAIL] LOG_DIRECT_BYTES: 日志未含全部写入内容")
                failures += 1
                s.log_tail()
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
