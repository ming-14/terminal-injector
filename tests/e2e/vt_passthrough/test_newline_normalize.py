"""特性: 直通流换行归一（裸 LF → CRLF）    类别: vt_passthrough

链路: 目标 SetConsoleMode(VT_PROCESSING) → WriteFile 写含裸 LF 的字节
      → DLL VtSgrFilter（Ground 态裸 LF 补 CR）→ mediator → WT

背景（2026-09-25 termtest 256 色段空行丢失修复）:
  ConHost 把裸 LF(0x0A) 当 CR+LF（回列 0 + 下移），WT/wezterm 只当纯 LF
  （下移、保持列号）。Phase 13 VT 直通原样转发裸 LF → WT 侧新行接在上一行
  末尾之后，列号逐行累加、触发自动换行多吃一行，视觉上空行被"挤掉"。
  归一化在 VtSgrFilter 内完成（唯一规范化出口，同时喂 VtCursorTracker）。

预期（以 ChildVtOutput 精确字节为准）:
  - 裸 LF → 线上 CR LF（AAA\\nBBB → 41 41 41 0D 0A 42 42 42）
  - 已是 CRLF → 原样，不得补成 CR CR LF
  - 连续裸 LF → 连续 CRLF
  - OSC 载荷内 0x0A 是数据，原样保留（OSC 仅由 BEL/ST 终止）

验证方式: mediator 日志 ChildVtOutput 精确字节正则
"""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "newline_normalize"

# 目标脚本：启用 VT 输出模式后，依次写入若干含 LF/CRLF/OSC 的字节段，
# 每段前记录一个 mark KEY，便于驱动侧分段断言。
TARGET_BODY = '''
rec("READY", "PASS")
time.sleep(2.0)  # 等 DLL 注入/LazyInit（避免启动竞态）
h_out = get_std_out()
ok = set_mode(h_out, ENABLE_VIRTUAL_TERMINAL_PROCESSING)
check("SET_VT_MODE", bool(ok), "err={}".format(ctypes.get_last_error()))

def seg(key, data):
    rec(key, "1")
    time.sleep(0.35)   # 各段独立成包，避免 BatchSender 合并
    write_bytes(h_out, data)
    time.sleep(0.35)

seg("SEG_LF", b"AAA\\nBBB")          # 裸 LF
seg("SEG_CRLF", b"CCC\\r\\nDDD")     # 已是 CRLF
seg("SEG_LF2", b"\\n\\n")            # 连续裸 LF
seg("SEG_OSC", b"\\x1b]0;a\\nb\\x07")  # OSC 载荷内 LF
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

            v = s.wait_result(NAME, "SET_VT_MODE", timeout=15.0)
            if v == "PASS":
                print("  [PASS] SET_VT_MODE")
            else:
                print("  [FAIL] SET_VT_MODE: {}".format(v or "no result"))
                failures += 1

            # 每段: (mark KEY, 期望正则, 说明, 反向禁止正则 or None)
            # 注意: DLL 每次写前补发 CursorPosition（独立包），故本段内容
            #       单独成一条 ChildVtOutput，长度即内容字节数。
            checks = [
                ("SEG_LF",
                 r"ChildVtOutput: len=8 written=8 ok=1 err=0 "
                 r"hex\[8\]=41 41 41 0D 0A 42 42 42",
                 "裸 LF 归一 (41 41 41 0D 0A 42 42 42)",
                 r"hex\[7\]=41 41 41 0A 42 42 42"),
                ("SEG_CRLF",
                 r"ChildVtOutput: len=8 written=8 ok=1 err=0 "
                 r"hex\[8\]=43 43 43 0D 0A 44 44 44",
                 "CRLF 原样 (43 43 43 0D 0A 44 44 44)",
                 r"hex\[9\]=43 43 43 0D 0D 0A 44 44 44"),
                ("SEG_LF2",
                 r"ChildVtOutput: len=4 written=4 ok=1 err=0 "
                 r"hex\[4\]=0D 0A 0D 0A",
                 "连续裸 LF → CRLF CRLF",
                 r"hex\[2\]=0A 0A"),
                ("SEG_OSC",
                 r"ChildVtOutput: len=8 written=8 ok=1 err=0 "
                 r"hex\[8\]=1B 5D 30 3B 61 0A 62 07",
                 "OSC 载荷内 LF 原样 (…61 0A 62 07)",
                 None),
            ]

            for key, want_re, desc, forbid_re in checks:
                if not s.wait_result(NAME, key, timeout=15.0):
                    print("  [FAIL] {}: mark 未出现".format(key))
                    failures += 1
                    continue
                m = log.wait_for_regex(want_re, timeout=8.0)
                if m:
                    print("  [PASS] {}: {}".format(key, desc))
                else:
                    print("  [FAIL] {}: 日志未见预期字节（8s 超时）".format(key))
                    failures += 1

            content = log.read_all()
            for key, _, desc, forbid_re in checks:
                if forbid_re and re.search(forbid_re, content):
                    print("  [FAIL] {}: 出现禁止字节 {}".format(key, forbid_re))
                    failures += 1
            if failures == 0:
                print("  [PASS] 反向断言：无裸 LF 残留 / 无 CR CR LF")
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
