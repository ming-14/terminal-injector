"""特性: KEY_EVENT_RECORD 全字段    类别: keyboard

链路: SendInput(press_key 真实按键) → WT → mediator → DLL 翻译 → ReadConsoleInputW

预期（press 'x' 的 down 事件）:
  - bKeyDown=True；wRepeatCount=1
  - wVirtualKeyCode == VkKeyScanW('x')（真实按键带 VK）
  - wVirtualScanCode 非零（扫描码）
  - uChar == 'x'
  - dwControlKeyState == 0（无修饰键）
  - up 事件 bKeyDown=False、其余同 down

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
from keyboard._common import build_reader, parse_keys

NAME = "input_record_fields"

VK_X = ctypes.windll.user32.VkKeyScanW("x") & 0xFF


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, build_reader(2), ready_key="READY")
            time.sleep(0.8)
            input_sim.press_key(VK_X)
            keys = parse_keys(s, NAME)
            if len(keys) < 2:
                print("  [FAIL] EVENT_COUNT: got {} expected 2".format(len(keys)))
                failures += 1
                s.log_tail()
                print("\nSUMMARY: {} ({} failures)".format(
                    "PASS" if failures == 0 else "FAIL", failures))
                return failures
            down, up = keys[0], keys[1]
            checks = [
                ("down", down["down"] is True, "bKeyDown"),
                ("repeat", down["repeat"] == 1, "wRepeatCount"),
                ("vk", down["vk"] == VK_X, "wVirtualKeyCode"),
                ("scan", down["scan"] != 0, "wVirtualScanCode"),
                ("char", down["char"] == "x", "uChar"),
                ("ctrl", down["ctrl"] == 0, "dwControlKeyState"),
                ("up", up["down"] is False, "up bKeyDown"),
            ]
            for label, ok, field in checks:
                if ok:
                    print("  [PASS] {} ({})".format(label, field))
                else:
                    print("  [FAIL] {}: {} (key={!r})".format(label, field, down if label != "up" else up))
                    failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
