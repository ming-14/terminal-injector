"""特性: 功能键 F1-F12    类别: keyboard

链路: SendInput(press_key) → WT → mediator → DLL 翻译 → ReadConsoleInputW

预期:
  - F1-F12 各产生 down+up 事件
  - down 事件 VK 码依次为 VK_F1(0x70)..VK_F12(0x7B)
  - 行编辑模式翻译为 INPUT_RECORD（VT 序列直通见 Phase 13 范畴）

限制:
  - F11(0x7A) 不测：Windows Terminal / conhost 默认把 F11 绑定为全屏快捷键，
    F11 序列被终端拦截不进 ConPTY（调试实证：两次批量测试均只丢 F11，
    且全屏切换触发 WtSizeWatcher 发 ResizeNotify）。属终端原生行为，非劫持缺陷。

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

NAME = "function_keys"

F_KEYS = [0x70 + i for i in range(10)] + [0x7B]


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, build_reader(len(F_KEYS) * 2), ready_key="READY")
            time.sleep(0.8)
            for vk in F_KEYS:
                input_sim.press_key(vk)
            keys = parse_keys(s, NAME)
            if len(keys) != len(F_KEYS) * 2:
                print("  [FAIL] EVENT_COUNT: got {} expected {}".format(
                    len(keys), len(F_KEYS) * 2))
                failures += 1
            else:
                print("  [PASS] EVENT_COUNT ({} 事件)".format(len(keys)))
            for i, vk in enumerate(F_KEYS):
                detail = assert_key(keys, i * 2, "F{}".format(i + 1), {
                    "down": True, "vk": vk})
                if detail:
                    print("  [FAIL] {}".format(detail))
                    failures += 1
                else:
                    print("  [PASS] F{} vk={:#x}".format(i + 1, vk))
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
