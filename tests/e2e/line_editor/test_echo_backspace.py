"""特性: 回显/退格/字符插入（cmd 行编辑）    类别: line_editor

链路: SendInput(type_text) → WT → mediator → DLL LineEditor → 回显 VT → WT

预期:
  - 在 cmd 提示符输入命令字符，WT 回显完整命令（mediator 日志 VtOutput 字节）
  - Backspace 删除字符，LineEditor 输出 CUB+擦除序列（1B 5B 31 44 1B 5B 30 4B）
  - 删除后重新插入字符正常
  - Enter 执行命令，重定向结果文件内容正确（命令执行结果）

验证方式: mediator 日志 VtOutput hex + cmd 重定向结果文件
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import paths
from common import result as result_mod

NAME = "echo_backspace"
RESULT = os.path.join(paths.RESULTS_DIR, NAME + ".txt")

# pipe→stdout: VtOutput len=N written=.. ok=.. err=.. hex[N]=XX XX ..
_VTOUT_RE = re.compile(
    r"pipe[^\r\n]*stdout: VtOutput len=\d+ written=\d+ ok=\d+ err=\d+ "
    r"hex\[\d+\]=((?:[0-9A-F]{2} )*)")


def vt_output_bytes(log: str) -> list:
    """提取日志中所有 VtOutput hex 字节，按出现顺序合并。"""
    out = []
    for m in _VTOUT_RE.finditer(log):
        hex_str = m.group(1).strip()
        if hex_str:
            out.extend(int(x, 16) for x in hex_str.split())
    return out


def contains_seq(stream: list, target: bytes) -> bool:
    """target 是否作为子序列按序出现在 stream 中。"""
    i = 0
    for b in stream:
        if i < len(target) and b == target[i]:
            i += 1
        if i >= len(target):
            return True
    return i >= len(target)


def wait_file_content(path: str, timeout: float = 10.0) -> str:
    """等待文件出现并返回 strip 后内容；超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    return f.read().strip()
            except OSError:
                pass
        time.sleep(0.2)
    return None


def run() -> int:
    result_mod.clear_result(NAME)
    try:
        os.remove(RESULT)
    except OSError:
        pass
    failures = 0
    try:
        with TestSession() as s:
            log = s.log()
            log.mark()
            time.sleep(0.5)

            # 输入命令主体（不回车、不含重定向）
            s.type_text("echo hello")
            time.sleep(1.0)
            content = log.read_new()

            # 1. 回显：VtOutput 字节流含 "echo hello"
            stream = vt_output_bytes(content)
            if contains_seq(stream, b"echo hello"):
                print("  [PASS] ECHO 回显完整命令字节")
            else:
                print("  [FAIL] ECHO: VtOutput 字节流缺 echo hello")
                failures += 1

            # 2. 退格 ×2（删行末 "lo"）：LineEditor 输出 CUB+擦除序列
            s.type_backspace()
            s.type_backspace()
            time.sleep(0.8)
            content2 = log.read_new()
            if "1B 5B 31 44 1B 5B 30 4B" in content2:
                print("  [PASS] BACKSPACE 退格编辑序列 (CUB+擦除)")
            else:
                print("  [FAIL] BACKSPACE: 日志未见 1B 5B 31 44 1B 5B 30 4B")
                failures += 1

            # 3. 补回 lo：插入字符回显
            s.type_text("lo")
            time.sleep(0.8)
            content3 = log.read_new()
            if contains_seq(vt_output_bytes(content3), b"lo"):
                print("  [PASS] INSERT 补插字符回显")
            else:
                print("  [FAIL] INSERT: 补插 lo 未回显")
                failures += 1

            # 4. 追加重定向并回车执行：echo hello > RESULT
            s.type_text(' > "{}"'.format(RESULT))
            s.type_enter()
            got = wait_file_content(RESULT, timeout=15.0)
            if got == "hello":
                print("  [PASS] EXEC 命令执行结果正确 (hello)")
            else:
                print("  [FAIL] EXEC: 结果文件={!r}（期望 hello）".format(got))
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
