"""子进程 DLL 日志解析工具（ChildExitSync / LazyInit aligned 基线）。

背景：子进程（python 等）注入后 ConsoleState 基线 = HelloAck 回传的
WT 真实光标（LazyInit "child cursor aligned to WT (X,Y) from HelloAck"，
2026-08-02 修复，见 PHASES.md LBUG-001）。此前基线是 ConHost 陈旧快照
(0,4)，X/Y 断言直接假设 0；现在基线随 WT 实际光标变化，测试需解析
aligned 值作为期望基线。
"""
import glob
import os
import re
import time

from common.paths import BUILD_BIN


def injected_log_dir() -> str:
    """DLL 注入日志目录（与 src/dll/LazyInit.cpp GetInjectedLogDir 对齐）。

    DLL 侧：TI_INJECTED_LOG_DIR 环境变量覆盖，否则 DLL 自身所在目录
    （默认部署下 injected.dll 与 exe 同目录 = build/bin/Release）。
    测试侧同样优先 TI_INJECTED_LOG_DIR，否则 BUILD_BIN（exe 目录），
    保证与目标进程（由测试经 mediator 启动，继承测试环境变量）一致。
    """
    return os.environ.get("TI_INJECTED_LOG_DIR") or BUILD_BIN


def injected_log_glob() -> str:
    """DLL 注入日志 glob 模式（文件名 injected_<pid>_<时间戳>.log）。"""
    return os.path.join(injected_log_dir(), "injected_*.log")


def latest_injected_log(pid: int) -> str:
    """返回指定 pid 最新一份 DLL 日志的路径（无则空串）。

    文件名带毫秒时间戳，同一 pid 多次注入/重复会话生成多个文件，
    按 mtime 取最新即本次会话。
    """
    pattern = os.path.join(injected_log_dir(), "injected_{}_*.log".format(pid))
    logs = sorted(glob.glob(pattern), key=os.path.getmtime)
    return logs[-1] if logs else ""


def find_child_aligned_baseline(timeout: float = 10.0) -> tuple:
    """从最新子进程 DLL 日志解析 LazyInit aligned 基线 (X, Y)。

    在最近创建的 injected_<pid>_<时间戳>.log 中找 "child cursor aligned to WT"
    记录（python 由测试启动，最新日志即本次测试的子进程）。
    未找到返回 None。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = sorted(glob.glob(injected_log_glob()),
                      key=os.path.getmtime, reverse=True)
        for lp in logs[:3]:
            try:
                with open(lp, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            m = re.search(r"child cursor aligned to WT \((\d+),(\d+)\)", content)
            if m:
                return (int(m.group(1)), int(m.group(2)))
        time.sleep(0.3)
    return None


def wait_child_exit_cursor(s_log, timeout: float = 20.0):
    """等待 mediator 日志出现 ChildExitSync 光标上报，返回 (X, Y) 或 None。

    s_log: TestSession 的日志对象（含 wait_for_regex）。
    """
    m = s_log.wait_for_regex(
        r"ChildExitSync sent cursor=\((\d+),(\d+)\)", timeout=timeout)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)))
