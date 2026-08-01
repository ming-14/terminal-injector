"""特性: 编辑键（Insert/Delete/Backspace/Tab/Esc）    类别: keyboard

链路: SendInput(press_key) → WT → mediator → DLL 翻译 → ReadConsoleInputW

预期:
  - 5 个编辑键各产生 down+up 事件
  - down 事件 VK 码依次为 VK_INSERT/DELETE/BACK/TAB/ESCAPE
  - Esc 事件的 uChar == '\\x1b'（Esc 产生转义字符）

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

NAME = "edit_keys"

EDIT = [
    ("insert", input_sim.VK_INSERT),
    ("delete", input_sim.VK_DELETE),
    ("backspace", input_sim.VK_BACK),
    ("tab", input_sim.VK_TAB),
    ("escape", input_sim.VK_ESCAPE),
]


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, build_reader(len(EDIT) * 2), ready_key="READY")
            time.sleep(0.8)
            for name, vk in EDIT:
                input_sim.press_key(vk)
            keys = parse_keys(s, NAME)
            if len(keys) != len(EDIT) * 2:
                print("  [FAIL] EVENT_COUNT: got {} expected {}".format(
                    len(keys), len(EDIT) * 2))
                failures += 1
            else:
                print("  [PASS] EVENT_COUNT ({} 事件)".format(len(keys)))
            for i, (name, vk) in enumerate(EDIT):
                expected = {"down": True, "vk": vk}
                if name == "escape":
                    expected["char"] = "\x1b"
                detail = assert_key(keys, i * 2, name, expected)
                if detail:
                    print("  [FAIL] {}".format(detail))
                    failures += 1
                else:
                    print("  [PASS] {} vk={:#x}".format(name, vk))
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
