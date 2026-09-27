"""e2e_v2 路径约定。

- V2_ROOT: 本套件根目录
- PROJECT_ROOT: 项目根（TI_PROJECT_ROOT 可覆盖，默认由本文件位置推导）
- pywezterm 目录: 首选 <project>/tests/vendor（AGENTS.md 指定），回退 reference
"""
import os

V2_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

PROJECT_ROOT = os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
    os.path.join(V2_ROOT, "..", ".."))

BUILD_BIN = os.path.join(PROJECT_ROOT, "build", "bin", "Release")
MEDIATOR_EXE = os.path.join(BUILD_BIN, "terminal_injector.exe")
INJECTED_DLL = os.path.join(BUILD_BIN, "injected.dll")
RELAY32_DLL = os.path.join(BUILD_BIN, "relay32.dll")

TARGETS_DIR = os.path.join(V2_ROOT, "_targets")
RESULTS_DIR = os.path.join(V2_ROOT, "_results")


def pywezterm_dir() -> str:
    """返回含 `pywezterm/` 子包的目录（可直接插进 sys.path）。

    PWTERM_DIR 优先；否则 tests/vendor（AGENTS.md 指定的新位置）→ reference（历史位置）。
    注意要指「含 pywezterm/ 子包的目录」，不是包目录本身：
    指错会报 `No module named 'pywezterm.pywezterm'`（看着像包坏了，其实只是路径不对）。
    """
    env = os.environ.get("PWTERM_DIR")
    if env:
        return env
    for cand in (os.path.join(PROJECT_ROOT, "tests", "vendor"),
                 os.path.join(PROJECT_ROOT, "reference")):
        if os.path.isdir(os.path.join(cand, "pywezterm")):
            return cand
    return os.path.join(PROJECT_ROOT, "tests", "vendor")


def mediator_log(target_pid: int) -> str:
    """mediator 日志（按目标 pid 分文件，并发会话互不干扰）。"""
    return os.path.join(BUILD_BIN, "logs", "terminal-injector-{}.log".format(target_pid))


def injected_log_dir() -> str:
    """DLL 注入日志目录（与 src/dll 侧 GetInjectedLogDir 对齐）。"""
    return os.environ.get("TI_INJECTED_LOG_DIR") or os.path.join(BUILD_BIN, "logs")


def ensure_dirs() -> None:
    for d in (TARGETS_DIR, RESULTS_DIR):
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)


def preflight() -> list:
    """检查运行前提，返回缺失项说明列表（空 = 齐备）。"""
    missing = []
    for p in (MEDIATOR_EXE, INJECTED_DLL):
        if not os.path.exists(p):
            missing.append("缺少构建产物 {}".format(p))
    if not os.path.isdir(os.path.join(pywezterm_dir(), "pywezterm")):
        missing.append("找不到 pywezterm 包（尝试过 {}）".format(pywezterm_dir()))
    return missing
