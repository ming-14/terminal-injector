"""特性: ENABLE_VIRTUAL_TERMINAL_PROCESSING（VT 输出直通）    类别: modes

链路: 目标 WriteFile(字节) → DLL WriteFile_Detour（VT 直通）→ mediator → WT

预期:
  - GetConsoleMode(输出句柄) 恒含 ENABLE_VIRTUAL_TERMINAL_PROCESSING（DLL 强制）
  - SetConsoleMode(输出句柄) 后 Get 返回 set|VT_PROCESSING（强制保留）
  - WriteFile 写 VT 序列字节原样直通（mediator VtOutput 日志字节含完整序列）

验证方式: 目标自检 get 模式 + mediator 日志 VtOutput hex
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "vt_output_mode"

_VTOUT_RE = re.compile(
    r"(?:ChildVtOutput: |pipe[^\r\n]*VtOutput )"
    r"len=\d+ written=\d+ ok=\d+ err=\d+ "
    r"hex\[\d+\]=((?:[0-9A-F]{2} )*)")


def vt_output_hex(log: str) -> str:
    out = []
    for m in _VTOUT_RE.finditer(log):
        hex_str = m.group(1).strip()
        if hex_str:
            out.append(hex_str)
    return " ".join(out)


TARGET_BODY = '''
rec("READY", "PASS")
# 等待 DLL 注入/LazyInit 完成，避免启动竞态（竞态期内 Hook 未生效，
# SetConsoleMode/WriteFile 走 pass-through，字节写真实 ConHost 丢失）
time.sleep(2.0)
h_out = get_std_out()
g0 = get_mode(h_out)
rec("GET_INIT", hex(g0))
ok1 = set_mode(h_out, ENABLE_PROCESSED_OUTPUT)
g1 = get_mode(h_out)
rec("SET_GET", str(int(ok1)) + " " + hex(g1))
# WriteFile 写 VT 颜色序列（VT 直通路径）
rec("BEFORE_WRITE", "1")
time.sleep(0.8)  # 给驱动 mark 时间，避免日志窗口竞态
ok2, nw = write_bytes(h_out, b"\\x1b[31mRED\\x1b[0m")
rec("WROTE", str(int(ok2)) + " " + str(nw))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            log = s.log()
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            v0 = s.wait_result(NAME, "GET_INIT", timeout=10.0)
            if not v0:
                print("  [FAIL] GET_INIT: 无结果")
                failures += 1
            else:
                mode = int(v0, 16)
                if mode & 0x4:
                    print("  [PASS] GET_INIT 输出模式含 VT_PROCESSING (0x{:x})".format(mode))
                else:
                    print("  [FAIL] GET_INIT: 输出模式无 VT_PROCESSING: {}".format(v0))
                    failures += 1

            v1 = s.wait_result(NAME, "SET_GET", timeout=10.0)
            if not v1:
                print("  [FAIL] SET_GET: 无结果")
                failures += 1
            else:
                parts = v1.split()
                if len(parts) == 2 and parts[0] == "1" and int(parts[1], 16) == 0x5:
                    print("  [PASS] SET_GET Set(0x1) 后 Get=0x5（强制保留 VT）")
                else:
                    print("  [FAIL] SET_GET: {}（期望 1 0x5）".format(v1))
                    failures += 1

            # 日志验证：VT 序列原样直通（等 BEFORE_WRITE 后 mark）
            # 管线异步：DLL→mediator→ConPTY→child 输出→日志，目标 done 后仍有延迟，
            # 故用 wait_for_regex 轮询等待序列出现而非立即 read_new
            v2 = s.wait_result(NAME, "BEFORE_WRITE", timeout=10.0)
            if not v2:
                print("  [FAIL] BEFORE_WRITE: 无结果")
                failures += 1
            log.mark()
            s.wait_result(NAME, "DONE", timeout=10.0)
            want = "1B 5B 33 31 6D 52 45 44 1B 5B 30 6D"
            m = log.wait_for_regex(
                r"ChildVtOutput: len=12 written=12 ok=1 err=0 "
                r"hex\[12\]=1B 5B 33 31 6D 52 45 44 1B 5B 30 6D",
                timeout=8.0)
            if m:
                print("  [PASS] VT 序列原样直通 (1B 5B 33 31 6D RED 1B 5B 30 6D)")
            else:
                print("  [FAIL] VT 直通: 日志未见完整序列（8s 超时）")
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
