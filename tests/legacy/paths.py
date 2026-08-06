#!/usr/bin/env python3
"""legacy 调试脚本统一路径解析（环境变量优先，占位符兜底）。

原则（工程规范：不硬编码路径）：
  - 项目根：TI_PROJECT_ROOT 环境变量，缺省按本文件位置相对解析
  - DLL 日志目录：TI_INJECTED_LOG_DIR，缺省 tempfile.gettempdir()
  - 调试器目录：TI_CDB_TOOLS，缺省 PROJECT_ROOT/.agents/skills/windows-debugging/<版本>
  - 符号缓存：TI_SYMBOL_PATH，缺省 srv*<TEMP>\\symbols*<微软符号服务器>
  - 崩溃转储目录：TI_DUMP_DIR，缺省 <TEMP>\\terminjector_dumps
  - PTY-Agent：PTY_AGENT_PATH，缺省占位符 <PTY_AGENT_PATH>
  - 输出目录：TI_LEGACY_OUT_DIR，缺省 <TEMP>\\terminjector_legacy_out

不再回退到任何本机用户名路径；环境变量未设置时用可用的系统位置。
"""
import glob
import os
import tempfile

MS_SYMBOL_SRV = "http://msdl.blackint3.com:88/download/symbols"

_DEFAULT_CDB_REL = os.path.normpath(
    r".agents\skills\windows-debugging\10.0.19041.5609")


def project_root() -> str:
    """项目根：TI_PROJECT_ROOT 或按 legacy/ 相对解析（上两级）。"""
    return os.environ.get("TI_PROJECT_ROOT") or os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def build_bin() -> str:
    """构建产物目录（Release）。"""
    return os.path.join(project_root(), "build", "bin", "Release")


def injected_log_dir() -> str:
    """DLL 注入日志目录（与 e2e common/childlog.py、src/dll LazyInit 对齐）。"""
    return os.environ.get("TI_INJECTED_LOG_DIR") or tempfile.gettempdir()


def injected_log(pid: int) -> str:
    """指定 pid 最新一份 DLL 日志路径（无则空串）。"""
    pattern = os.path.join(injected_log_dir(), "injected_{}_*.log".format(pid))
    logs = sorted(glob.glob(pattern), key=os.path.getmtime)
    return logs[-1] if logs else ""


def injected_log_glob() -> str:
    """DLL 日志 glob（全部 pid）。"""
    return os.path.join(injected_log_dir(), "injected_*.log")


def cdb_tools() -> str:
    """cdb 工具目录：TI_CDB_TOOLS 或项目 .agents 下默认版本。"""
    return os.environ.get("TI_CDB_TOOLS") or os.path.join(
        project_root(), _DEFAULT_CDB_REL)


def cdb_exe() -> str:
    """cdb.exe 全路径。"""
    return os.path.join(cdb_tools(), "cdb.exe")


def symbol_path() -> str:
    """符号路径：TI_SYMBOL_PATH 或 srv*<temp>\\symbols*<符号服务器>。"""
    return os.environ.get("TI_SYMBOL_PATH") or "srv*{}*{}".format(
        os.path.join(tempfile.gettempdir(), "symbols"), MS_SYMBOL_SRV)


def dump_dir() -> str:
    """崩溃转储目录：TI_DUMP_DIR 或 <temp>\\terminjector_dumps。"""
    return os.environ.get("TI_DUMP_DIR") or os.path.join(
        tempfile.gettempdir(), "terminjector_dumps")


def pty_agent() -> str:
    """PTY-Agent app.py：PTY_AGENT_PATH 或占位符（未配置时报错提示）。"""
    v = os.environ.get("PTY_AGENT_PATH")
    if v:
        return v
    raise RuntimeError("PTY_AGENT_PATH 未设置（legacy 脚本依赖外部 PTY-Agent 项目）")


def out_dir() -> str:
    """legacy 输出目录：TI_LEGACY_OUT_DIR 或 <temp>\\terminjector_legacy_out。"""
    return os.environ.get("TI_LEGACY_OUT_DIR") or os.path.join(
        tempfile.gettempdir(), "terminjector_legacy_out")
