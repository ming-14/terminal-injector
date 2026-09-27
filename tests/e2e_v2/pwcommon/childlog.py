"""进程与日志侧的小工具：模块表、DLL 日志定位、子进程光标上报。

对应 v1 的 common/childlog.py，但去掉只能配合 v1 日志对象的接口：
v2 的 session 直接提供 wait_log/wait_log_regex，这里只保留"取文件/解析文本"。
"""
import glob
import os
import re
import time

from . import paths

HOOK_DLLS = ("injected.dll", "relay32.dll")


# ---------------------------------------------------------------- 模块表
def modules(pid: int) -> set:
    """进程已加载模块名集合（小写）。psutil.memory_maps 足以覆盖 DLL 判定。"""
    try:
        import psutil
        return {os.path.basename(m.path).lower()
                for m in psutil.Process(pid).memory_maps()}
    except Exception:
        return set()


def has_hook_dll(pid: int) -> bool:
    """该进程里有没有我们的劫持 DLL（认主 DLL 与 32 位中继）。"""
    mods = modules(pid)
    return any(d in mods for d in HOOK_DLLS)


def wait_module_state(pid: int, name: str, present: bool,
                      timeout: float = 10.0, interval: float = 0.3) -> bool:
    """等待进程模块表里出现/消失某个模块。返回是否达到期望状态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if (name.lower() in modules(pid)) == present:
            return True
        time.sleep(interval)
    return (name.lower() in modules(pid)) == present


# ---------------------------------------------------------------- DLL 日志
def injected_log(pid: int) -> str:
    """指定 pid 最新一份 DLL 日志的路径（无则空串）。

    文件名 injected_<pid>_<时间戳>.log；同一 pid 多次注入会有多份，按 mtime 取最新。
    """
    pattern = os.path.join(paths.injected_log_dir(), "injected_{}_*.log".format(pid))
    cands = sorted(glob.glob(pattern), key=os.path.getmtime)
    return cands[-1] if cands else ""


def latest_injected_log() -> str:
    """全目录最新一份 DLL 日志（不知道 pid 时用，如子进程日志）。"""
    cands = sorted(glob.glob(os.path.join(paths.injected_log_dir(), "injected_*.log")),
                   key=os.path.getmtime)
    return cands[-1] if cands else ""


def read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return ""


def snapshot_injected_logs() -> set:
    """当前已有 DLL 日志路径集合（用于"会话开始后新增的那份 = 子进程日志"）。"""
    return set(glob.glob(os.path.join(paths.injected_log_dir(), "injected_*.log")))


def aligned_baseline(pid: int = 0, timeout: float = 10.0):
    """解析 `child cursor aligned to WT (X,Y)`，返回 (X, Y) 或 None。

    pid 给 0 时在最新几个日志文件里找（python 子进程由测试启动，最新即本次）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pid:
            cands = [injected_log(pid)] if injected_log(pid) else []
        else:
            cands = sorted(glob.glob(os.path.join(paths.injected_log_dir(), "injected_*.log")),
                           key=os.path.getmtime, reverse=True)[:3]
        for lp in cands:
            m = re.search(r"child cursor aligned to WT \((\d+),(\d+)\)", read(lp))
            if m:
                return (int(m.group(1)), int(m.group(2)))
        time.sleep(0.3)
    return None


def wait_child_exit_cursor(mediator_text_or_session, timeout: float = 20.0):
    """等待 mediator 侧 `ChildExitSync sent cursor=(X,Y)`，返回 (X, Y) 或 None。

    接受两种入参：v2 的 session（有 wait_log_regex）或日志全文（字符串）。
    传字符串时是"一次性匹配"，不打等待——需要等待就传 session。
    """
    pat = r"ChildExitSync sent cursor=\((\d+),(\d+)\)"
    if hasattr(mediator_text_or_session, "wait_log_regex"):
        m = mediator_text_or_session.wait_log_regex(pat, timeout=timeout)
    else:
        m = re.search(pat, mediator_text_or_session or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))
