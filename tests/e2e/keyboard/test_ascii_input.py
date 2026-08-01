"""特性: ASCII 字符输入（a-z/0-9）    类别: keyboard

链路: SendInput(type_text) → WT → mediator → DLL 翻译 → 目标 ReadConsoleInputW

预期:
  - 输入 "abc123" 产生 12 个事件（每字符 down+up）
  - down 事件 uChar 依次为 a/b/c/1/2/3，VK 码为对应 VkKeyScanW 值
  - 每个 down 都有成对 up（bKeyDown=False）

验证方式: 目标脚本读 KEY_EVENT_RECORD 记录到结果文件
"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod
from helpers import input_sim
from keyboard._common import build_reader, parse_keys, assert_key

NAME = "ascii_input"
TEXT = "abc123"


def _vk_of(ch: str) -> int:
    return ctypes.windll.user32.VkKeyScanW(ch) & 0xFF


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, build_reader(len(TEXT) * 2), ready_key="READY")
            time.sleep(0.8)
            input_sim.type_text(TEXT)
            keys = parse_keys(s, NAME)
            if len(keys) != len(TEXT) * 2:
                print("  [FAIL] EVENT_COUNT: got {} expected {}".format(
                    len(keys), len(TEXT) * 2))
                failures += 1
            else:
                print("  [PASS] EVENT_COUNT ({} 事件)".format(len(keys)))
            for i, ch in enumerate(TEXT):
                detail = assert_key(keys, i * 2, "KEY[{}]".format(i * 2), {
                    "down": True, "char": ch, "vk": _vk_of(ch)})
                if detail:
                    print("  [FAIL] {}".format(detail))
                    failures += 1
                else:
                    print("  [PASS] KEY[{}] uChar={!r} vk={:#x}".format(
                        i * 2, ch, _vk_of(ch)))
                detail = assert_key(keys, i * 2 + 1, "KEY[{}]".format(i * 2 + 1), {
                    "down": False, "char": ch})
                if detail:
                    print("  [FAIL] {}".format(detail))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
