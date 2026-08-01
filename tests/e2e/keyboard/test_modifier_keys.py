"""特性: 修饰键组合（Ctrl/Alt/Shift，Ctrl+C 除外）    类别: keyboard

链路: SendInput(press_combo) → WT → mediator → DLL 翻译 → ReadConsoleInputW

预期:
  - Shift+X：down 事件 VK=VK_X、字符为大写 'X'（证明 shift 生效）
    ctrl 不要求 SHIFT_PRESSED：WT→ConPTY 文本流中 shift 信息丢失
    （大写字符无法区分 Shift/CapsLock；方向键+shift 同样无标志，已实测）
  - Ctrl+A：ctrl 含 LEFT_CTRL_PRESSED(0x08)（控制字符 \\x01 可推断）
  - Alt+X：ctrl 含 LEFT_ALT_PRESSED(0x02)（ESC 前缀可推断）
  - 每组合 down+up 各一事件

环境注意（2026-08-02）:
  - 中文输入法开启时，SendInput 的修饰键组合会被 IME 截走组词，
    目标收不到事件（EVENT_COUNT=0）——测试 setup 时先关闭 WT 窗口
    IME（ImmSetOpenStatus(false)，见 keyboard/_common.py disable_ime）。

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
from keyboard._common import build_reader, parse_keys, assert_key, disable_ime

NAME = "modifier_keys"

SHIFT_PRESSED = 0x0010
LEFT_ALT_PRESSED = 0x0002
LEFT_CTRL_PRESSED = 0x0008


def _vk_of(ch: str) -> int:
    return ctypes.windll.user32.VkKeyScanW(ch) & 0xFF


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    combos = [
        ("shift+x", [input_sim.VK_SHIFT, _vk_of("x")], SHIFT_PRESSED, _vk_of("x"), "X"),
        ("ctrl+a", [input_sim.VK_CONTROL, 0x41], LEFT_CTRL_PRESSED, 0x41, "\x01"),
        ("alt+x", [input_sim.VK_MENU, _vk_of("x")], LEFT_ALT_PRESSED, _vk_of("x"), "x"),
    ]
    try:
        with TestSession() as s:
            s.run_target(NAME, build_reader(len(combos) * 2), ready_key="READY")
            time.sleep(0.8)
            disable_ime()
            time.sleep(0.2)
            for name, vks, flag, vk, char in combos:
                input_sim.press_combo(vks)
            keys = parse_keys(s, NAME)
            if len(keys) != len(combos) * 2:
                print("  [FAIL] EVENT_COUNT: got {} expected {}".format(
                    len(keys), len(combos) * 2))
                failures += 1
            else:
                print("  [PASS] EVENT_COUNT ({} 事件)".format(len(keys)))
            for i, (name, vks, flag, vk, char) in enumerate(combos):
                detail = assert_key(keys, i * 2, name, {"down": True, "vk": vk})
                if detail:
                    print("  [FAIL] {}".format(detail))
                    failures += 1
                # 事件不足时不再做 ctrl 标志检查（避免空 keys 假 PASS）
                if len(keys) <= i * 2:
                    continue
                k = keys[i * 2]
                if (k.get("ctrl", 0) & flag) == 0:
                    if name == "shift+x":
                        if k.get("char", "") != char:
                            print("  [FAIL] {}: char={!r} 期望大写 {!r}".format(
                                name, k.get("char", ""), char))
                            failures += 1
                        else:
                            print("  [PASS] {} 字符 {}（shift 大小写生效；ctrl 标志上游丢失）".format(
                                name, char))
                    else:
                        print("  [FAIL] {}: ctrl={:#x} 缺标志 {:#x}".format(
                            name, k.get("ctrl", 0), flag))
                        failures += 1
                else:
                    print("  [PASS] {} ctrl 标志 {:#x} 命中".format(name, flag))
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
