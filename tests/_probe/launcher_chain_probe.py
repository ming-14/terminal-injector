# -*- coding: utf-8 -*-
"""launcher_chain_probe.py —— 逐环验证 py.exe 启动器链路（Phase 23 端到端）

链路
----
    ps7(64) ──注入──► injected.dll
       └─ CreateProcess ─► py.exe(32)   ← 32 位存根，x64 DLL 注不进
              └─ CreateProcess ─► python.exe(64)  ← 真正跑用户脚本的进程

修好之后每一环都应成立：
    L1  py.exe 内加载 relay32.dll（由 relay32inject.exe 注入）
    L2  relay32 连上 mediator 并发出 RelayHello（会话首帧）
    L3  relay32 捕获 64 位子进程 → 发 RelayChildNotify 给 mediator
    L4  mediator 把 injected.dll 注入被冻住的 python.exe，并回 RelayChildAck
    L5  python.exe 恢复执行，其输出经 DLL→mediator 到达终端（字节可见）
    L6  整条链不挂死：py.exe 的 CreateProcessW 在合理时间内返回

为什么用 pywezterm 真 ConPTY 而不是 WT+SendInput：
    焦点无关、秒级、能直接读真实字节。详见 pywezterm-terminal-probe 技能。

    python launcher_chain_probe.py [存活秒数，默认 25]
"""
import ctypes
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

BUILD_BIN = os.path.join(os.environ["TI_PROJECT_ROOT"], "build", "bin", "Release")
LOG_DIR = os.path.join(BUILD_BIN, "logs")
MARKER = "TI_PY_CHAIN_OK"


# ------------------------------------------------------------------ 工具
def image_bitness(path):
    """用 GetBinaryTypeW 判定位数（OS 权威；手工解析 PE 易错）。"""
    bt = ctypes.c_ulong(0)
    ok = ctypes.windll.kernel32.GetBinaryTypeW(
        ctypes.c_wchar_p(path), ctypes.byref(bt))
    if not ok:
        return None
    return {0: 32, 6: 64}.get(bt.value, bt.value)


def modules_of(pid):
    """进程已加载模块名集合（小写）。"""
    try:
        import psutil
        return {os.path.basename(m.path).lower()
                for m in psutil.Process(pid).memory_maps()}
    except Exception:
        return set()


def procs_named(pid, name):
    """目标 pid 的后代里匹配 name 的进程（按 pid 升序）。"""
    import psutil
    out = []
    try:
        for c in psutil.Process(pid).children(recursive=True):
            if c.name().lower() == name:
                out.append(c.pid)
    except Exception:
        pass
    return sorted(out)


def tail(path, n=400):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()[-n * 40:]
    except OSError:
        return ""


def newest_log(prefix, pid):
    """找 <prefix>_<pid>_*.log 里最新的一份。"""
    pat = re.compile(re.escape(prefix) + r"_" + str(pid) + r"_(\d{8}-\d{6}(-\d{3})?)\.log")
    cands = []
    if os.path.isdir(LOG_DIR):
        for f in os.listdir(LOG_DIR):
            m = pat.fullmatch(f)
            if m:
                cands.append((m.group(1), f))
    if not cands:
        # relay32inject 的日志名只有 pid，没有时间戳
        alt = os.path.join(LOG_DIR, "{}-{}.log".format(prefix, pid))
        return alt if os.path.exists(alt) else None
    cands.sort()
    return os.path.join(LOG_DIR, cands[-1][1])


# ------------------------------------------------------------------ 主流程
def main():
    alive = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0

    print("=" * 74)
    print("py.exe 启动器链路逐环验证   (存活观察 {:.0f}s)".format(alive))
    print("=" * 74)

    # 前提自检：链路的两个端点位数必须真的不同，否则本探针测不到目标路径
    pyexe = r"C:\Windows\py.exe"
    py_bits = image_bitness(pyexe) if os.path.exists(pyexe) else None
    print("[前提] {} 位数 = {}".format(pyexe, py_bits))
    if py_bits != 32:
        print("  [SKIP] py.exe 不是 32 位（本机无 32 位启动器），链路不含跨位数环节")
        return 0
    pyver = os.popen('py -3 -c "import struct;print(struct.calcsize(\'P\')*8)"').read().strip()
    print("[前提] py -3 解析到的 python 位数 = {}".format(pyver))
    if pyver != "64":
        print("  [SKIP] py 默认解析到 32 位 python，链路为 32→32（另一条路径）")
        return 0

    fails = []
    with PwSession() as s:
        if not s.start("ps7"):
            print("[FAIL] 握手失败（mediator 未就绪）")
            return 1
        print("[setup] 握手 OK  target_pid={} mediator_pid={}".format(
            s.target_pid, s.mediator_pid))
        s.drain(1.5)

        # ---- 敲入 py -3 调用（脚本落盘，避免内联 -c 的嵌套引号问题）
        script = os.path.join(LOG_DIR, "launcher_chain_payload.py")
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(script, "w", encoding="utf-8") as f:
            f.write("import time\n"
                    "print({!r}, flush=True)\n"
                    "time.sleep({})\n".format(MARKER, alive))
        s.write_line('py -3 "{}"'.format(script))

        # ---- 采样一：等 3s，看 py.exe 是否已被注入 relay32
        time.sleep(3.0)
        py_pids = procs_named(s.target_pid, "py.exe")
        py_pid = py_pids[0] if py_pids else 0
        mods = modules_of(py_pid) if py_pid else set()
        print("\n[采样 3s] py.exe pid={}".format(py_pid or "(未捕获)"))
        if py_pid and "relay32.dll" in mods:
            print("  ✓ L1 py.exe 内已加载 relay32.dll")
        else:
            print("  ✗ L1 py.exe 内没有 relay32.dll；已加载含 relay 的模块={}".format(
                sorted(m for m in mods if "relay" in m or "inject" in m)))
            fails.append("L1")

        med = s.mediator_log()
        relay_pid_match = re.search(
            r"Handshake: RelayHello pid=(\d+) bitness=(\d+)", med)
        if relay_pid_match and int(relay_pid_match.group(1)) == py_pid:
            print("  ✓ L2 mediator 收到 RelayHello pid={} bitness={}".format(
                relay_pid_match.group(1), relay_pid_match.group(2)))
        else:
            print("  ✗ L2 mediator 日志未见来自 py.exe({}) 的 RelayHello".format(py_pid))
            fails.append("L2")

        # ---- 采样二：再等 6s，看 relay 有没有把 64 位孙进程报上来
        time.sleep(6.0)
        med = s.mediator_log()
        renot = re.search(r"OnRelayChildNotify: childPid=(\d+) parentPid=(\d+)", med)
        py_pids2 = procs_named(s.target_pid, "py.exe")
        py_pid2 = py_pids2[0] if py_pids2 else py_pid
        py_pid = py_pid2
        relay_log = tail(newest_log("relay32", py_pid) or "", 400)
        notes = [l for l in relay_log.splitlines()
                 if "RelayChildNotify" in l or "is 64-bit" in l or "ack=" in l]

        print("\n[采样 9s] relay32 日志")
        for l in notes:
            print("    " + l[l.find("]") + 1:].strip()[:140] if "]" in l else "    " + l)
        if renot:
            print("  ✓ L3 mediator 收到 RelayChildNotify child={} parent={}".format(
                renot.group(1), renot.group(2)))
            child_pid = int(renot.group(1))
        else:
            print("  ✗ L3 mediator 未收到 RelayChildNotify（relay 的 Send 未送达）")
            fails.append("L3")
            child_pid = (procs_named(s.target_pid, "python.exe") or [0])[0]

        # ---- L4/L5：孙进程是否被注入 + 输出是否到达终端
        if child_pid:
            cmods = modules_of(child_pid)
            if "injected.dll" in cmods:
                print("  ✓ L4 python.exe({}) 内已加载 injected.dll".format(child_pid))
                if re.search(r"RelayChildAck: child={}\b".format(child_pid), relay_log):
                    print("     · relay 侧收到 RelayChildAck（mediator 已完成注入）")
            else:
                print("  ✗ L4 python.exe({}) 内没有 injected.dll；含 inject 的模块={}".format(
                    child_pid, sorted(m for m in cmods if "inject" in m or "relay" in m)))
                fails.append("L4")
        else:
            print("  ✗ L4 未找到 python.exe 孙进程")
            fails.append("L4")

        got = s.drain(3.0)
        if MARKER.encode() in s.all_output():
            print("  ✓ L5 终端收到 python.exe 输出标记 {!r}（输出被劫持到 ConPTY）".format(MARKER))
        else:
            print("  ✗ L5 终端未见到 {!r}（最近字节={!r}）".format(
                MARKER, bytes(s.all_output()[-160:])))
            fails.append("L5")

        # ---- L6：链是否挂死（py.exe 的 CreateProcessW 是否返回并恢复）
        print("\n[采样 {}s] 存活性与最终状态".format(int(alive)))
        time.sleep(max(0.0, alive - 12.0))
        s.drain(2.0)
        alive_py = bool(procs_named(s.target_pid, "py.exe"))
        relay_log = tail(newest_log("relay32", py_pid) or "", 400)
        med_end = s.mediator_log()
        if re.search(r"child \d+ ack=\d", relay_log) or \
           re.search(r"RelayChildAck: child=", relay_log):
            print("  ✓ L6 relay 侧 detour 有返回（有 ack 记录），链路未挂死")
        else:
            print("  ✗ L6 relay 侧 detour 无返回记录，CreateProcessW 仍被堵")
            fails.append("L6")
        print("     · py.exe 进程仍存活={}（探针结束会清理）".format(alive_py))

    print("\n" + "=" * 74)
    if fails:
        print("RESULT: FAIL  失败环节 = {}".format(", ".join(fails)))
    else:
        print("RESULT: PASS  6/6 环节全部成立")
    print("=" * 74)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
