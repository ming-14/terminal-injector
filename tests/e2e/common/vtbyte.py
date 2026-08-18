"""VT 原始字节输出测试公共逻辑（vt_output 类别）。

VT 直通模式下虚拟状态不跟踪输出（程序自维护状态，Phase 13 设计），
因此本类测试统一以 mediator 日志 VtOutput/ChildVtOutput 字节为唯一验证依据：

  目标脚本 SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)
  → WriteFile 写原始 VT 序列 → DLL 直通 → mediator 日志 hex 应包含完整序列

用法：
    from common.vtbyte import run_vt_byte_test
    SEQS = [(b"\\x1b[1m", "LOG_BOLD"), ...]
    def run():
        return run_vt_byte_test("sgr_styles", SEQS)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod


def to_hex(data: bytes) -> str:
    """字节 → 大写空格分隔 hex（与 mediator 日志格式一致）。"""
    return " ".join("{:02X}".format(b) for b in data)


def build_body(seqs) -> str:
    """由序列规格生成目标脚本正文。

    seqs: [(bytes, key)]，每项一个日志断言 KEY。
    """
    lines = [
        'rec("READY", "PASS")',
        "h_out = get_std_out()",
        "ok = set_mode(h_out, ENABLE_VIRTUAL_TERMINAL_PROCESSING)",
        'check("SET_VT_MODE", bool(ok), "err={}".format(ctypes.get_last_error()))',
    ]
    for item in seqs:
        data = item[0]
        lines.append("write_bytes(h_out, {!r})".format(bytes(data)))
    lines.append("done()")
    return "\n".join(lines)


def check_log_bytes(log, patterns, timeout: float = 10.0) -> int:
    """验证 mediator 日志中 hex 模式的出现/缺席。

    多个序列可能合并在同一条 VtOutput 日志行：先 wait_for 第一个必须
    出现的模式，再用 read_all() 全文匹配其余模式（避免 read_new offset
    越过旧内容）。
    patterns: [(hex_str, key)] 或 [(hex_str, key, "absent")]。
    缺省为"必须出现"；标记 "absent" 的断言该 hex 不得出现在日志中
    （用于验证 DLL 入口剥离 ConHost 无法表达的属性，如删除线 SGR 9/29）。
    返回失败数。
    """
    present = [(h, k) for h, k, *rest in patterns if not rest or rest[0] != "absent"]
    absent = [(h, k) for h, k, *rest in patterns if rest and rest[0] == "absent"]

    failures = 0
    if not present:
        # 全部是缺席断言：等待写入到达后读取全文检查
        time.sleep(1.0)
        content = log.read_all()
        for hex_str, key in absent:
            if hex_str in content:
                print("  [FAIL] {}: 日志不应出现 {}".format(key, hex_str))
                failures += 1
            else:
                print("  [PASS] {} (VtOutput 字节缺席)".format(key))
        return failures

    first_hex, first_key = present[0]
    if not log.wait_for(first_hex, timeout=timeout):
        print("  [FAIL] {}: 日志未出现 {}".format(first_key, first_hex))
        return 1
    print("  [PASS] {} (VtOutput 字节命中)".format(first_key))
    content = log.read_all()
    for hex_str, key in present[1:]:
        if hex_str in content:
            print("  [PASS] {} (VtOutput 字节命中)".format(key))
        else:
            print("  [FAIL] {}: 日志未出现 {}".format(key, hex_str))
            failures += 1
    for hex_str, key in absent:
        if hex_str in content:
            print("  [FAIL] {}: 日志不应出现 {}".format(key, hex_str))
            failures += 1
        else:
            print("  [PASS] {} (VtOutput 字节缺席)".format(key))
    return failures


def run_vt_byte_test(name: str, seqs, handshake_timeout: float = 20.0,
                     ready_timeout: float = 30.0) -> int:
    """完整运行一个 VT 字节测试。seqs: [(bytes, key)] 或 [(bytes, key, "absent")]。

    断言：
      1. SET_VT_MODE: 目标脚本成功启用 VT 输出模式
      2. 每个序列的 hex 出现（或缺席）于 mediator 日志（VtOutput/ChildVtOutput）
    """
    result_mod.clear_result(name)
    failures = 0
    try:
        with TestSession(handshake_timeout=handshake_timeout) as s:
            s.run_target(name, build_body(seqs), ready_key="READY",
                         ready_timeout=ready_timeout)
            v = s.wait_result(name, "SET_VT_MODE", timeout=10.0)
            if v == "PASS":
                print("  [PASS] SET_VT_MODE")
            else:
                print("  [FAIL] SET_VT_MODE: {}".format(v or "no result"))
                failures += 1
            patterns = []
            for item in seqs:
                data, key = item[0], item[1]
                if len(item) >= 3:
                    patterns.append((to_hex(data), key, item[2]))
                else:
                    patterns.append((to_hex(data), key))
            failures += check_log_bytes(s.log(), patterns)
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures
