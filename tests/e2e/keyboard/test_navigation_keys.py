"""特性: 导航键（方向/Home/End/PageUp/PageDown）    类别: keyboard

链路: SendInput(press_key) → WT → mediator → DLL 翻译 → ReadConsoleInputW

预期:
  - 8 个导航键各产生 down+up 事件
  - down 事件 VK 码依次为 VK_LEFT/UP/RIGHT/DOWN/HOME/END/PRIOR/NEXT

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

NAME = "navigation_keys"

NAV = [
    ("left", input_sim.VK_LEFT),
    ("up", input_sim.VK_UP),
    ("right", input_sim.VK_RIGHT),
    ("down", input_sim.VK_DOWN),
    ("home", input_sim.VK_HOME),
    ("end", input_sim.VK_END),
    ("pgup", input_sim.VK_PRIOR),
    ("pgdn", input_sim.VK_NEXT),
]


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, build_reader(len(NAV) * 2), ready_key="READY")
            time.sleep(0.8)
            for name, vk in NAV:
                input_sim.press_key(vk)
            keys = parse_keys(s, NAME)
            if len(keys) != len(NAV) * 2:
                print("  [FAIL] EVENT_COUNT: got {} expected {}".format(
                    len(keys), len(NAV) * 2))
                failures += 1
            else:
                print("  [PASS] EVENT_COUNT ({} 事件)".format(len(keys)))
            for i, (name, vk) in enumerate(NAV):
                detail = assert_key(keys, i * 2, name, {"down": True, "vk": vk})
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
