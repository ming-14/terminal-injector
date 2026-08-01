"""特性: cmd Tab 补全    类别: line_editor

链路: SendInput(type_text/type_tab) → WT → mediator → DLL TabCompleter → 回显补全

预期:
  - 预置唯一前缀文件 tabcomplete_marker.txt
  - 输入 `type tabcomplete` + Tab → 补全为 `type tabcomplete_marker.txt`
  - 补全后的命令执行结果正确（结果文件验证）

验证方式: mediator 日志回显字节 + cmd 重定向结果文件
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import paths
from common import result as result_mod

NAME = "tab_completion"
MARKER = "tabcomplete_marker.txt"
MARKER_CONTENT = "TAB_COMPLETION_OK"
RESULT = os.path.join(paths.RESULTS_DIR, NAME + ".txt")

_VTOUT_RE = re.compile(
    r"pipe[^\r\n]*stdout: VtOutput len=\d+ written=\d+ ok=\d+ err=\d+ "
    r"hex\[\d+\]=((?:[0-9A-F]{2} )*)")


def vt_output_bytes(log: str) -> list:
    out = []
    for m in _VTOUT_RE.finditer(log):
        hex_str = m.group(1).strip()
        if hex_str:
            out.extend(int(x, 16) for x in hex_str.split())
    return out


def contains_seq(stream: list, target: bytes) -> bool:
    i = 0
    for b in stream:
        if i < len(target) and b == target[i]:
            i += 1
        if i >= len(target):
            return True
    return i >= len(target)


def wait_file_content(path: str, timeout: float = 10.0) -> str:
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
    marker_path = os.path.join(paths.TARGETS_DIR, MARKER)
    with open(marker_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(MARKER_CONTENT)
    failures = 0
    try:
        with TestSession() as s:
            log = s.log()
            time.sleep(0.5)

            # 1. 进入 _targets 目录（Tab 补全枚举当前目录）
            s.type_text('cd "{}"'.format(paths.TARGETS_DIR))
            s.type_enter()
            time.sleep(1.0)

            # 2. 输入 type tabcomplete + Tab
            log.mark()
            s.type_text("type tabcomplete")
            time.sleep(0.5)
            s.type_tab()
            time.sleep(1.0)

            # 3. 回显验证：补全后的行出现完整文件名
            content = log.read_new()
            if contains_seq(vt_output_bytes(content), MARKER.encode("utf-8")):
                print("  [PASS] TAB 补全回显 tabcomplete_marker.txt")
            else:
                print("  [FAIL] TAB: 日志未见补全后的文件名")
                failures += 1

            # 4. 重定向并执行：type tabcomplete_marker.txt > RESULT
            s.type_text(' > "{}"'.format(RESULT))
            s.type_enter()
            got = wait_file_content(RESULT, timeout=15.0)
            if got == MARKER_CONTENT:
                print("  [PASS] EXEC 补全命令执行结果正确")
            else:
                print("  [FAIL] EXEC: 结果文件={!r}（期望 {}）".format(got, MARKER_CONTENT))
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
