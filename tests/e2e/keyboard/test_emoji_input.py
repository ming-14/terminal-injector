"""特性: Emoji 代理对输入（BMP 外字符）    类别: keyboard

链路: SendInput(高代理 + 低代理) → WT → mediator → DLL 翻译 → ReadConsoleInputW

预期:
  - 输入 "😀"(U+1F600) 产生 4 个事件（高代理 down/up + 低代理 down/up）
  - 两个 down 事件的 uChar 组合（高代理 + 低代理）合成 U+1F600
  - 合成验证：surrogate 对 → 码点 == ord("😀")

验证方式: 目标脚本读 KEY_EVENT_RECORD 记录到结果文件
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from helpers import input_sim
from keyboard._common import build_reader, parse_keys, assert_key

NAME = "emoji_input"
EMOJI = "😀"
CP = ord(EMOJI)
HIGH = 0xD800 + ((CP - 0x10000) >> 10)
LOW = 0xDC00 + ((CP - 0x10000) & 0x3FF)


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, build_reader(4), ready_key="READY")
            time.sleep(0.8)
            input_sim.type_char(EMOJI)
            keys = parse_keys(s, NAME)
            if len(keys) != 4:
                print("  [FAIL] EVENT_COUNT: got {} expected 4".format(len(keys)))
                failures += 1
            else:
                print("  [PASS] EVENT_COUNT (4 事件)")
            detail = assert_key(keys, 0, "KEY[0]", {
                "down": True, "char": chr(HIGH), "vk": 0})
            if detail:
                print("  [FAIL] {}".format(detail))
                failures += 1
            else:
                print("  [PASS] KEY[0] 高代理 {:#06x}".format(HIGH))
            detail = assert_key(keys, 2, "KEY[2]", {
                "down": True, "char": chr(LOW), "vk": 0})
            if detail:
                print("  [FAIL] {}".format(detail))
                failures += 1
            else:
                print("  [PASS] KEY[2] 低代理 {:#06x}".format(LOW))
            if len(keys) >= 4:
                merged = ord(keys[0]["char"]) * 0x400 + ord(keys[2]["char"]) + 0x10000 - 0xD800 * 0x400 - 0xDC00
                # 简化：直接按 UTF-16 代理合成
                merged = 0x10000 + ((ord(keys[0]["char"]) - 0xD800) << 10) + (ord(keys[2]["char"]) - 0xDC00)
                if merged == CP:
                    print("  [PASS] MERGE_CP (合成码点 {:#06x})".format(merged))
                else:
                    print("  [FAIL] MERGE_CP: merged {:#06x} expected {:#06x}".format(merged, CP))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
