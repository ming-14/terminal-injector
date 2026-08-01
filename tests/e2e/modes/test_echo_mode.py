"""特性: ENABLE_ECHO_INPUT 开关    类别: modes

链路: SendInput(type_text) → WT → mediator → DLL LineEditor（echoEnabled 由模式决定）→ 回显 VT

预期:
  - ECHO 开启：ReadConsoleW 行编辑回显输入字符（VtOutput 字节流含字符）
  - ECHO 关闭：ReadConsoleW 仍返回输入内容，但无回显（VtOutput 无字符）
  - 两种模式返回内容一致（ok=1 n=2 字符+\\r\\n）

验证方式: mediator 日志 VtOutput 字节 + 目标 ReadConsoleW 自检
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "echo_mode"

_VTOUT_RE = re.compile(
    r"(?:ChildVtOutput: |pipe[^\r\n]*VtOutput )"
    r"len=\d+ written=\d+ ok=\d+ err=\d+ "
    r"hex\[\d+\]=((?:[0-9A-F]{2} )*)")


def vt_output_bytes(log: str) -> list:
    out = []
    for m in _VTOUT_RE.finditer(log):
        hex_str = m.group(1).strip()
        if hex_str:
            out.extend(int(x, 16) for x in hex_str.split())
    return out


TARGET_BODY = '''
rec("READY", "PASS")
h_in = get_std_in()
# ECHO 开
set_mode(h_in, ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT | ENABLE_ECHO_INPUT)
_k.FlushConsoleInputBuffer(h_in)
buf = ctypes.create_unicode_buffer(64)
n = wintypes.DWORD(0)
ok = _k.ReadConsoleW(h_in, buf, 63, ctypes.byref(n), None)
if ok:
    rec("ECHO_ON_RET", str(n.value) + " " + buf.value.encode("utf-8").hex())
else:
    rec("ECHO_ON_RET", "FAIL")
# ECHO 关（保持 LINE/PROCESSED）
set_mode(h_in, ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT)
_k.FlushConsoleInputBuffer(h_in)
buf2 = ctypes.create_unicode_buffer(64)
n2 = wintypes.DWORD(0)
ok2 = _k.ReadConsoleW(h_in, buf2, 63, ctypes.byref(n2), None)
if ok2:
    rec("ECHO_OFF_RET", str(n2.value) + " " + buf2.value.encode("utf-8").hex())
else:
    rec("ECHO_OFF_RET", "FAIL")
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

            # ECHO 开阶段：输入 "a" + Enter → 回显 a（VtOutput 含 61）
            log.mark()
            s.type_text("a")
            s.type_enter()
            v = s.wait_result(NAME, "ECHO_ON_RET", timeout=15.0)
            if not v:
                print("  [FAIL] ECHO_ON_RET: 无结果（ReadConsoleW 未返回？）")
                failures += 1
            else:
                parts = v.split()
                if len(parts) == 2 and parts[0] == "3" and parts[1] == "610d0a":
                    print("  [PASS] ECHO_ON 收到 a\\r\\n (n=3)")
                else:
                    print("  [FAIL] ECHO_ON_RET: {}（期望 3 610d0a）".format(v))
                    failures += 1
            stream1 = vt_output_bytes(log.read_new())
            if 0x61 in stream1:
                print("  [PASS] ECHO_ON 回显字符 a (VtOutput 含 61)")
            else:
                print("  [FAIL] ECHO_ON: VtOutput 未回显 a")
                failures += 1

            # ECHO 关阶段：输入 "b" + Enter → 无回显 b（VtOutput 不含 62）
            log.mark()
            s.type_text("b")
            s.type_enter()
            v2 = s.wait_result(NAME, "ECHO_OFF_RET", timeout=15.0)
            if not v2:
                print("  [FAIL] ECHO_OFF_RET: 无结果（ReadConsoleW 未返回？）")
                failures += 1
            else:
                parts = v2.split()
                if len(parts) == 2 and parts[0] == "3" and parts[1] == "620d0a":
                    print("  [PASS] ECHO_OFF 仍收到 b\\r\\n (n=3)")
                else:
                    print("  [FAIL] ECHO_OFF_RET: {}（期望 3 620d0a）".format(v2))
                    failures += 1
            stream2 = vt_output_bytes(log.read_new())
            if 0x62 in stream2:
                print("  [FAIL] ECHO_OFF: 关闭回显后 VtOutput 仍出现 b")
                failures += 1
            else:
                print("  [PASS] ECHO_OFF 关闭后无回显 b")
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    import time
    sys.exit(run())
