# -*- coding: utf-8 -*-
"""ab_probe.py —— 子进程注入的 A/B 对照探针（技能自带示例 / 环境自检）。

两端：
  A 控制组 python.exe 直接子进程 → 期望 hooked=True（injected.dll）
  B 实验组 py.exe 启动器子进程   → 引入 32 位中继后**期望已改变**（见下）

历史：引入 relay32 之前，B 组应为 `hooked=False`（x64 DLL 注不进 32 位存根），
"A 成功 / B 失败"正是探针能正确判别的证据。

现在（relay32 已落地）：B 组应变成
  py.exe      hooked=True  —— 挂的是 relay32.dll
  python.exe  hooked=True  —— 挂的是 injected.dll（由 mediator 接力注入）
所以本脚本不再作"A 成 B 败"的判别基准，只作环境自检与人工观察。
要**逐环断言**跨位数链路，用项目里的 `tests/_probe/launcher_chain_probe.py`。

    python ab_probe.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pwterm import PwSession


def run_case(s, cmd, label, expect_hooked):
    print("\n" + "=" * 68)
    print("[case] {}   (期望 hooked={})".format(label, expect_hooked))
    print("=" * 68)

    s.clear_output()
    s.write_line(cmd)
    time.sleep(2.0)                       # 在子进程存活期内采样

    rows = [(p, n, h) for (p, n, h) in s.tree()
            if n.lower() not in ("conhost.exe",)]
    for pid, name, hooked in rows:
        # 用 modules() 看清挂的到底是主 DLL 还是中继
        mods = sorted(m for m in s.modules(pid) if "inject" in m or "relay" in m)
        print("   pid={:<6} {:<14} hooked={:<5} dll={}".format(
            pid, name, hooked, mods or "(无)"))

    s.drain(5.0)
    for line in s.grep("injected", ("InjectDllToChild", "OnChildProcessCreated")):
        print("    ", line[:130])

    return rows


def main():
    with PwSession() as s:
        if not s.start("ps7"):
            print("握手失败")
            return 1
        print("[setup] 握手 OK target_pid={} mediator_pid={}".format(
            s.target_pid, s.mediator_pid))
        s.drain(1.5)

        run_case(s, 'python -c "import time;time.sleep(6)"',
                 "A 控制组: python.exe", True)
        time.sleep(2)
        run_case(s, 'py -c "import time;time.sleep(6)"',
                 "B 实验组: py.exe 启动器", True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
