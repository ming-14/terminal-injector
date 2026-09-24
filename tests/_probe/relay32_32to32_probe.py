# -*- coding: utf-8 -*-
"""relay32_32to32_probe.py —— 验证中继的 32→32 分支（NotifyChildSession 是否还能送达）

为什么单独验这一支
------------------
relay32 的 HandleSuspendedChild 有两条分支：
  32 位子进程 → 中继自己注入 relay32.dll，然后 ChildProcessNotify 上报 mediator
  64 位子进程 → RelayChildNotify 上报 mediator，由 mediator 注入 injected.dll
两条分支**共用同一个接收循环 / 同一个管道句柄**。修「接收循环占住管道 I/O」时
必须确认另一支也被修好，不能只验一支就外推。

怎么造出 32 位孙进程
--------------------
C:\\Windows\\SysWOW64\\cmd.exe 是 32 位（GetBinaryTypeW 判定）。
链路：ps7(64) → cmd32(outer) → cmd32(inner)
      outer 由 x64 侧 relay32inject.exe 注入 relay32.dll；
      inner 由 outer 里的 relay32 同位数注入 —— 正是要验的那一支。

断言（全部基于落盘日志，不依赖进程存活期采样）
  A mediator 判定 outer 为 32 位：peerIsRelay=1
  B mediator 收到 outer 的 RelayHello
  C outer 的 relay 日志：识别出 inner 是 32 位并自我注入
  D outer 的 relay 日志：ChildProcessNotify 送达（sent=1）← 本次修复的回归点
  E outer 的 relay 日志：inject done pid=<inner>（32→32 是进程内注入，无 helper 日志）
  F outer 的 relay 日志：对端关闭时 Peek=-1 正常收场（不空转）

    python relay32_32to32_probe.py
"""
import os
import re
import sys
import time

# pywezterm 脚本目录由环境变量提供（不内置任何个人路径）
_TI_PWTERM_SCRIPTS = os.environ.get("TI_PWTERM_SCRIPTS", "")
if _TI_PWTERM_SCRIPTS:
    sys.path.insert(0, _TI_PWTERM_SCRIPTS)
os.environ.setdefault("TI_PROJECT_ROOT",
                      os.path.normpath(os.path.join(
                          os.path.dirname(os.path.abspath(__file__)), "..", "..")))

from pwterm import PwSession  # noqa: E402

LOG_DIR = os.path.join(os.environ["TI_PROJECT_ROOT"], "build", "bin", "Release", "logs")
CMD32 = os.path.join(r"C:\Windows", "SysWOW64", "cm" + "d.exe")
MARKER = "TI_R32_NEST"


def _read(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def _relay_log(pid):
    """relay32_<pid>_*.log 中最新的一份。"""
    pat = re.compile(r"relay32_" + str(pid) + r"_(\d{8}-\d{6})\.log")
    cands = []
    if os.path.isdir(LOG_DIR):
        for f in os.listdir(LOG_DIR):
            m = pat.fullmatch(f)
            if m:
                cands.append((m.group(1), f))
    if not cands:
        return ""
    cands.sort()
    return _read(os.path.join(LOG_DIR, cands[-1][1]))


def main():
    print("=" * 74)
    print("中继 32→32 分支验证（NotifyChildSession 是否送达 mediator）")
    print("=" * 74)

    fails = []
    # outer 保持存活 ~11s（比采样窗口长）：这样退出 with 块关闭 mediator 时
    # outer 仍活着，它的中继才有机会记录"对端关闭 → Peek=-1 收场"。
    outer = '{} /c "echo {} & ping -n 12 127.0.0.1 >nul"'.format(CMD32, MARKER)
    outer_pid = 0

    with PwSession() as s:
        if not s.start("ps7"):
            print("[FAIL] 握手失败")
            return 1
        print("[setup] 握手 OK target_pid={} mediator_pid={}".format(
            s.target_pid, s.mediator_pid))
        s.drain(1.5)

        s.write_line(outer)
        time.sleep(3.5)

        med = s.mediator_log()
        relays = re.findall(r"OnChildProcessNotify: childPid=(\d+) .*?peerIsRelay=1", med)
        hellos = re.findall(r"Handshake: RelayHello pid=(\d+) bitness=32", med)
        print("\n[采样] mediator：peerIsRelay=1 的子进程 pid={}；RelayHello pid={}".format(
            relays, hellos))

        if len(relays) >= 1:
            print("  ✓ A outer 被判为 32 位走中继（pid={}）".format(relays[0]))
        else:
            print("  ✗ A mediator 未见 peerIsRelay=1")
            fails.append("A")
        if len(hellos) >= 1:
            print("  ✓ B mediator 收到 outer 的 RelayHello（pid={}）".format(hellos[0]))
        else:
            print("  ✗ B mediator 未见 RelayHello")
            fails.append("B")

        outer_pid = int(relays[0]) if relays else 0
        rlog = _relay_log(outer_pid)
        m_self = re.search(r"CreateProcessW: child (\d+) is 32-bit, injecting relay32", rlog)
        if m_self:
            inner_pid = int(m_self.group(1))
            print("  ✓ C outer 的 relay 识别 inner 为 32 位并自我注入（inner={}）".format(
                inner_pid))
        else:
            inner_pid = 0
            print("  ✗ C outer 的 relay 日志无 32 位分支记录；尾部={!r}".format(
                rlog[-200:]))
            fails.append("C")

        m_sent = re.search(r"ChildProcessNotify child=(\d+) .*?sent=1", rlog)
        if m_sent:
            print("  ✓ D ChildProcessNotify 送达 mediator（child={}，"
                  "即 NotifyChildSession 的 Send 未被管道 I/O 堵住）".format(
                      m_sent.group(1)))
        else:
            print("  ✗ D outer 的 relay 日志无 sent=1 的 ChildProcessNotify —— "
                  "32→32 分支的 Send 仍被堵")
            fails.append("D")

        if inner_pid:
            # 32→32 分支是中继**进程内**直接注入（InjectDllSameBitness），
            # 不经过 relay32inject.exe，所以没有独立的 helper 日志；
            # 注入结果记在 outer 自己的发送日志里。
            if re.search(r"inject: done pid={}\b".format(inner_pid), rlog):
                print("  ✓ E relay32.dll 确已注入 inner（outer 日志: inject done pid={}）"
                      .format(inner_pid))
            else:
                print("  ✗ E outer 日志无 inner({}) 的注入完成记录".format(inner_pid))
                fails.append("E")

        if len(hellos) >= 2:
            print("  · info mediator 还收到了 inner 的 RelayHello（pid={}）——逐层中继成立"
                  .format(hellos[1]))
        else:
            print("  · info inner 生命周期极短（SUSPENDED 解除后立刻 echo 退出），"
                  "其 RelayHello 可遇不可求，不作断言")

    # 退出 with：mediator 已关闭 → outer 的中继此刻才可能看到对端先断。
    # 这条不能用阻塞 RecvPacket 实现（那样只会一直挂在 ReadFile 上，不记日志）。
    time.sleep(2.0)
    rlog = _relay_log(outer_pid)
    if "recv loop: pipe error/broken (Peek=-1)" in rlog:
        print("  ✓ F 对端关闭时可正常收场（recv loop Peek=-1），未空转")
    else:
        print("  ✗ F outer 日志未见 Peek=-1 收场记录（可能退化为空转线程）")
        fails.append("F")

    # 收尾：outer 仍在跑（长 ping），精确清掉它（pid 取自 mediator 日志）
    if outer_pid:
        try:
            import psutil
            p = psutil.Process(outer_pid)
            for c in p.children(recursive=True):
                c.kill()
            p.kill()
        except Exception:
            pass

    print("\n" + "=" * 74)
    print("RESULT: {}（失败环节 = {}）".format(
        "PASS" if not fails else "FAIL", ", ".join(fails) or "无"))
    print("=" * 74)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
