"""特性: 模式状态机一致性（多轮 set/get + VT_INPUT 切换清队列）    类别: modes

链路: 目标 SetConsoleMode/GetConsoleMode（DLL ModeHooks）+ 输入驱动

预期:
  - 输入句柄：任意模式 set → get 一致（10 轮固定种子随机模式）
  - 输出句柄：set → get == set | ENABLE_VIRTUAL_TERMINAL_PROCESSING（强制）
  - 队列清空语义（2026-08-30 修正，对齐 Phase 13 设计）：
    仅当 ENABLE_VIRTUAL_TERMINAL_INPUT(0x200) 标志变化时清空输入队列
    （翻译↔透传切换，残留记录会按错误语义被读取）；非 VT_INPUT 模式
    切换（如 msvcrt.getwch 临时 raw 读）不清队，注入的字符保留。

验证方式: 目标自检逐轮记录 + 驱动注入字符后按 VT_INPUT 变化断言队列
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.session import TestSession
from common import result as result_mod

NAME = "mode_sync"

# 输入侧候选模式：覆盖各标志组合（固定种子，可复现）
TARGET_BODY = '''
rec("READY", "PASS")
h_in = get_std_in()
h_out = get_std_out()
rng = 12345
def rnd():
    global rng
    rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
    return rng
cands = [0x0, 0x1, 0x2, 0x3, 0x4, 0x5, 0x6, 0x7, 0x40, 0x47, 0x20, 0x240, 0x27F]
ok_all = True
for i in range(10):
    m = cands[rnd() % len(cands)]
    set_mode(h_in, m)
    g = get_mode(h_in)
    rec("M{:02d}".format(i), hex(m) + " " + hex(g))
    if g != m:
        ok_all = False
    if i == 3:
        # 第 4 轮前留窗口给驱动注入字符，随后 set 切换：
        #   VT_INPUT 标志变化 → 清空队列；否则保留注入字符
        rec("STEP3", "1")
        time.sleep(3.0)
        before = g  # STEP3 切换前的模式（i==3 刚 set 的）
        set_mode(h_in, cands[rnd() % len(cands)])
        time.sleep(0.3)
        ev = read_input_records(h_in, 8, peek=True)
        new_g = get_mode(h_in)
        vt_changed = ((before & 0x200) != 0) != ((new_g & 0x200) != 0)
        rec("QUEUE_AFTER_SWITCH", str(len(ev)))
        rec("VT_CHANGED", str(int(vt_changed)))
for i in range(3):
    m = cands[rnd() % len(cands)]
    set_mode(h_out, m)
    g = get_mode(h_out)
    rec("O{:d}".format(i), hex(m) + " " + hex(g))
rec("OK_ALL", str(int(ok_all)))
done()
'''


def run() -> int:
    result_mod.clear_result(NAME)
    failures = 0
    try:
        with TestSession() as s:
            s.run_target(NAME, TARGET_BODY, ready_key="READY")
            time.sleep(0.5)

            # 等 STEP3 出现后注入 "z"（将进入队列，随后按模式切换语义断言）
            v_step = s.wait_result(NAME, "STEP3", timeout=20.0)
            if not v_step:
                print("  [FAIL] STEP3: 目标未到达第 4 轮")
                failures += 1
            else:
                s.type_text("z")
                print("  [INFO] 已注入 z，等待目标模式切换")

            # 逐轮断言 set/get 一致
            for i in range(10):
                v = s.wait_result(NAME, "M{:02d}".format(i), timeout=20.0)
                if not v:
                    print("  [FAIL] M{:02d}: 无结果".format(i))
                    failures += 1
                else:
                    parts = v.split()
                    if len(parts) == 2 and parts[0] == parts[1]:
                        continue
                    print("  [FAIL] M{:02d}: {} set!=get".format(i, v))
                    failures += 1
            print("  [INFO] 输入侧 10 轮 set/get 校验完成")

            # 队列清空语义：VT_INPUT 标志变化 → 清空（0）；否则保留注入的 z
            v_q = s.wait_result(NAME, "QUEUE_AFTER_SWITCH", timeout=20.0)
            v_c = s.wait_result(NAME, "VT_CHANGED", timeout=20.0)
            if not v_q or not v_c:
                print("  [FAIL] QUEUE_AFTER_SWITCH/VT_CHANGED: 无结果")
                failures += 1
            else:
                expect = "0" if v_c == "1" else "2"  # z 按下+释放两条记录
                if v_q == expect:
                    print("  [PASS] 队列语义正确: VT_CHANGED={} 队列={} "
                          "(期望 {})".format(v_c, v_q, expect))
                else:
                    print("  [FAIL] QUEUE_AFTER_SWITCH: {} VT_CHANGED={} "
                          "（期望 {}）".format(v_q, v_c, expect))
                    failures += 1

            # 输出侧：set → get == set|0x4
            for i in range(3):
                v = s.wait_result(NAME, "O{:d}".format(i), timeout=20.0)
                if not v:
                    print("  [FAIL] O{:d}: 无结果".format(i))
                    failures += 1
                else:
                    parts = v.split()
                    if len(parts) == 2 and int(parts[1], 16) == (int(parts[0], 16) | 0x4):
                        continue
                    print("  [FAIL] O{:d}: {}（期望 get==set|0x4）".format(i, v))
                    failures += 1
            print("  [INFO] 输出侧 3 轮 set/get 校验完成")

            v_ok = s.wait_result(NAME, "OK_ALL", timeout=10.0)
            if v_ok == "1":
                print("  [PASS] OK_ALL 输入侧全部一致")
            else:
                print("  [FAIL] OK_ALL: {}".format(v_ok))
                failures += 1
    except RuntimeError as e:
        print("  [FAIL] setup 失败: {}".format(e))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
