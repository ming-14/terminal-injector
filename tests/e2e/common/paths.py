"""e2e 全局路径约定。

- TESTS_ALL_ROOT: 本测试套件根目录
- TARGETS_DIR / RESULTS_DIR: 目标脚本与结果文件运行时目录
- PROJECT_ROOT: terminal-injector 项目根（环境变量 TI_PROJECT_ROOT 可覆盖）
"""
import os

TESTS_ALL_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
TARGETS_DIR = os.path.join(TESTS_ALL_ROOT, "_targets")
RESULTS_DIR = os.path.join(TESTS_ALL_ROOT, "results")

# 项目根：优先 TI_PROJECT_ROOT 环境变量；否则按 e2e 位置相对解析
# （e2e 位于 <project>/tests/e2e → 项目根 = 上级上级），不硬编码机器路径
PROJECT_ROOT = os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
    os.path.join(TESTS_ALL_ROOT, "..", ".."))
BUILD_BIN = os.path.join(PROJECT_ROOT, "build", "bin", "Release")

# mediator 日志（vt_capture 使用）
TI_LOG_PATH = os.path.join(BUILD_BIN, "terminal-injector.log")


def ensure_dirs() -> None:
    """确保运行时目录存在。"""
    for d in (TARGETS_DIR, RESULTS_DIR):
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
