# discover.py — terminal_injector.exe / injected.dll 自动探测
#
# 为什么需要它:布局约定要求 exe/dll 与 gui.py(或打包后的 exe)同目录,
# 但实际部署经常把它们塞进子目录。缺失时从程序自身目录出发做一次受限
# 递归搜索,找到即由 backend 回填路径,免去手工摆放或报错。
#
# 三条硬约束:
#   1. 限制深度   —— SEARCH_MAX_DEPTH,防深链/超长路径把扫描拖死
#   2. 排除脏目录 —— 缓存/依赖树/VCS/IDE/系统目录;node_modules 这类目录
#                    动辄数十万文件,一旦误入 20s 预算会被整段烧光
#   3. 后台线程   —— find_binaries 是阻塞原语,只允许在工作线程调用;
#                    GUI 侧一律经 tasks.TaskRunner.run_readonly 派发
#                    (见 app.AppGui.rediscover),绝不在主线程里跑。
#
# 可配置项就是本文件顶部的常量(直接改);find_binaries 的同名参数可按次
# 覆盖,便于测试时传小深度/短超时。

import fnmatch
import os
import time
from collections import deque
from pathlib import Path
from typing import Dict, NamedTuple

from .i18n import _t
from .paths import resolve_program_dir

# ==================== 可配置项(改这里即可) ====================

SEARCH_MAX_DEPTH = 30      # 递归深度上限;0 = 只看程序目录本身
SEARCH_TIMEOUT = 20.0      # 搜索超时(秒);<= 0 = 不限时
SEARCH_ROOT = None         # 搜索根;None = 程序自身目录(见 resolve_program_dir)

# ==================== 目录排除表 ====================
#
# 比较一律小写(Windows 文件系统大小写不敏感)。
# 故意【不】排除构建输出目录(dist/build/bin/out/target/x64/Debug/Release),
# terminal_injector.exe 完全可能就躺在那里面。

EXCLUDED_DIRS = frozenset({
    # --- Python 生态 ---
    "__pycache__", ".venv", "venv", ".virtualenv", "virtualenv", ".env",
    ".tox", ".nox", ".eggs", "site-packages", "__pypackages__",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".hypothesis", ".pytype",
    ".pyre", ".dmypy_cache", "pip-wheel-metadata",
    # --- Node / 前端 ---
    "node_modules", "bower_components", "jspm_packages",
    ".pnpm", ".pnpm-store", ".pnpm-debug", ".yarn", ".npm", ".npx",
    ".next", ".nuxt", ".svelte-kit", ".angular", ".parcel-cache",
    # --- 版本控制 ---
    ".git", ".svn", ".hg", ".bzr", ".darcs", "_darcs", "cvs",
    # --- IDE / 编辑器 / 远程开发 ---
    ".idea", ".vscode", ".vscode-server", ".vs", ".eclipse", ".cursor",
    ".claude", ".qoder",
    # --- 缓存 / 日志 / 临时 ---
    ".cache", ".temp", "temp", "tmp", ".tmp", "logs", "log",
    "coverage", "htmlcov", ".nyc_output", ".sass-cache",
    # --- 其他包管理器 / 工具缓存 ---
    ".gradle", ".m2", ".nuget", ".cargo", ".stack", ".cabal", ".rvm",
    ".rbenv", ".maven", ".docker", ".terraform",
    # --- 依赖树 / 第三方 ---
    "vendor", "third_party", "thirdparty",
    # --- 系统 / 卷 ---
    "$recycle.bin", "system volume information", "windows.old",
})

# 目录名通配排除(fnmatch,已小写化)
EXCLUDED_PATTERNS = (
    "*.egg-info", "*.dist-info", "*.egg", "*.src.zip",
    "__pycache__*", ".ipynb_checkpoints",
)

# 重解析点属性:符号链接 / 目录联接(junction)。联接指向祖先会绕回成环,
# 深度限制只能兜底不能根治,直接不进去。
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400


class DiscoveryResult(NamedTuple):
    """一次探测的结果;found 为本次新找到的『原始文件名 -> 绝对路径』"""

    found: Dict[str, Path]   # 类体注解会被求值,用 typing.Dict 保持 3.6+ 兼容
    root: Path
    elapsed: float = 0.0
    scanned: int = 0          # 访问过的条目数(文件+目录)
    timed_out: bool = False   # True = 到点收工,可能还有目录没走完
    error: str = ""           # 非空 = 根本没法开始(根目录不存在等)


def _is_excluded(name: str) -> bool:
    """目录名是否命中排除表(精确 + 通配,均忽略大小写)"""
    lowered = name.lower()
    if lowered in EXCLUDED_DIRS:
        return True
    return any(fnmatch.fnmatch(lowered, pat) for pat in EXCLUDED_PATTERNS)


def _is_reparse(entry) -> bool:
    """目录是否为重解析点(符号链接 / junction),是则不进入"""
    if os.name != "nt":
        try:
            return entry.is_symlink()
        except OSError:
            return False
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError, ValueError):
        return False
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def current_root() -> Path:
    """本次探测实际会用的搜索根:SEARCH_ROOT 覆盖优先,否则程序自身目录

    日志与扫描必须同源,否则配置了 SEARCH_ROOT 时启动日志会报错的根目录。
    """
    return Path(SEARCH_ROOT) if SEARCH_ROOT is not None \
        else resolve_program_dir()


def describe_limits(max_depth: int = SEARCH_MAX_DEPTH,
                    timeout: float = SEARCH_TIMEOUT) -> str:
    """搜索限制的人类可读描述(写日志用)"""
    if timeout is None or timeout <= 0:
        t = _t("disc_unlimited")
    else:
        t = _t("disc_seconds").format(f"{timeout:g}")
    return _t("disc_limits").format(max_depth, t)


def find_binaries(names, root=None, max_depth: int = SEARCH_MAX_DEPTH,
                  timeout: float = SEARCH_TIMEOUT) -> DiscoveryResult:
    """在 root 下广度优先递归搜索 names 中的文件名(**阻塞**)

    广度优先保证先命中浅层 —— 程序目录自身的副本优先于深层子目录;
    全部 names 命中即提前返回。超时(或深度走完)时返回已扫到的部分并置
    timed_out,由调用方决定怎么提示。

    只允许在工作线程调用,见模块头「后台线程」约束。
    """
    root_path = Path(root) if root is not None else current_root()
    started = time.monotonic()
    deadline = started + timeout if timeout and timeout > 0 else None

    # 小写名 -> 原始名;find_binaries 只负责报告调用方给的原名
    wanted = {}
    for n in names:
        wanted.setdefault(n.lower(), n)
    found: Dict[str, Path] = {}

    if not root_path.is_dir():
        return DiscoveryResult({}, root_path, 0.0, 0, False,
                               _t("disc_bad_root").format(root_path))

    scanned = 0
    timed_out = False
    queue = deque([(str(root_path), 0)])   # (目录, 深度);0 = 程序目录本身

    while queue:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True
            break
        dirpath, depth = queue.popleft()
        try:
            it = os.scandir(dirpath)
        except OSError:
            continue                       # 无权限 / 中途消失:跳过不算错
        with it:
            for entry in it:
                scanned += 1
                # 每 64 项核一次时钟:microtask 级别够灵敏,又不拖慢扫描
                if deadline is not None and scanned % 64 == 0 \
                        and time.monotonic() >= deadline:
                    timed_out = True
                    break
                try:
                    if entry.is_file():
                        key = entry.name.lower()
                        if key in wanted:
                            found.setdefault(wanted[key], Path(entry.path))
                            if len(found) == len(wanted):
                                break
                        continue
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if not is_dir or depth >= max_depth:
                    continue
                if _is_excluded(entry.name) or _is_reparse(entry):
                    continue
                queue.append((entry.path, depth + 1))
        if timed_out or len(found) == len(wanted):
            break

    return DiscoveryResult(found, root_path, time.monotonic() - started,
                           scanned, timed_out, "")
