"""特性: 模式状态机一致性（多轮 set/get + 切换清队列）    类别: modes

链路: 目标 SetConsoleMode/GetConsoleMode（DLL ModeHooks）+ 输入驱动

预期:
  - 输入句柄：任意模式 set → get 一致（10 轮固定种子随机模式）
  - 输出句柄：set → get == set | ENABLE_VIRTUAL_TERMINAL_PROCESSING（强制）
  - 模式切换清空输入队列：切换前注入的字符被清除，切换后 ReadConsoleInputW 无残留

验证方式: 目标自检逐轮记录 + 驱动注入字符后断言队列清空
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
        # 第 4 轮前留窗口给驱动注入字符，随后 set 切换应清空队列
        rec("STEP3", "1")
        time.sleep(3.0)
        set_mode(h_in, cands[rnd() % len(cands)])
        time.sleep(0.3)
        ev = read_input_records(h_in, 8, peek=True)
        rec("QUEUE_AFTER_SWITCH", str(len(ev)))
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

            # 等 STEP3 出现后注入 "z"（将进入队列，随后被模式切换清空）
            v_step = s.wait_result(NAME, "STEP3", timeout=20.0)
            if not v_step:
                print("  [FAIL] STEP3: 目标未到达第 4 轮")
                failures += 1
            else:
                s.type_text("z")
                print("  [INFO] 已注入 z，等待目标切换清队列")

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

            v_q = s.wait_result(NAME, "QUEUE_AFTER_SWITCH", timeout=20.0)
            if not v_q:
                print("  [FAIL] QUEUE_AFTER_SWITCH: 无结果")
                failures += 1
            else:
                if v_q == "0":
                    print("  [PASS] 切换清队列：注入的 z 已清除")
                else:
                    print("  [FAIL] QUEUE_AFTER_SWITCH: {}（期望 0，队列有残留）".format(v_q))
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
