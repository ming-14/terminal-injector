# paths.py — exe/dll 定位
#
# 布局约定:terminal_injector.exe 与 injected.dll 须与 gui.py 同目录。
# 不能用 Path(__file__).resolve().parent:包内的 __file__ 指向 tigui/,
# 故统一走 resolve_base_dir() 解析。
#
# 基目录解析顺序:
#   1. PyInstaller onefile 解压目录(sys._MEIPASS)
#   2. 入口脚本所在目录(sys.argv[0])—— 双击 / 快捷方式 / 带路径运行
#      (以 python -m tigui.app 加载时 argv[0] 落在包内,跳过本项)
#   3. 包目录的上一级(兜底:被 import 方式加载)
#
# 另有 resolve_program_dir():递归搜索起点 —— 打包后 = exe 所在目录,
# 源码态 = gui.py 所在目录(取包上级,不看 argv[0])。与 resolve_base_dir()
# 分工不同(前者「从哪开始搜」,后者「按约定去哪找」),别混用。

import sys
from pathlib import Path

EXE_NAME = "terminal_injector.exe"
DLL_NAME = "injected.dll"


def resolve_program_dir() -> Path:
    """返回程序自身所在目录:打包后 = exe 所在目录,源码运行 = gui.py 所在目录

    与 resolve_base_dir() 的分工:后者优先取 sys._MEIPASS(onefile 解压出的
    临时目录,内置 exe/dll 的落点),适合「按约定找文件」;本函数取的是真实
    发布位置,适合「从程序这里开始往下搜」—— 用户把 terminal_injector.exe
    丢在打包后的 exe 旁边时,探测必须从那个目录出发,而不是 _MEIPASS。

    源码态直接取包上级(tigui/ 的上一级),不看 sys.argv[0]:gui.py 无论被
    双击、`python -m` 还是被别的脚本 import,所在目录都是同一个;而 argv[0]
    会随入口脚本漂移(见 resolve_base_dir 第 2 级),拿来做搜索根不可靠。
    """
    if getattr(sys, "frozen", False) or getattr(sys, "_MEIPASS", None):
        exe = getattr(sys, "executable", "")
        if exe:
            return Path(exe).resolve().parent
    return Path(__file__).resolve().parent.parent   # gui.py 所在目录


def resolve_base_dir() -> Path:
    """返回 exe/dll 所在目录(即 gui.py 所在目录)"""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    pkg_dir = Path(__file__).resolve().parent      # <base>/tigui
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0:
        cand = Path(argv0).resolve().parent
        if cand != pkg_dir:                        # -m 加载时 argv[0] 在包内
            return cand
    return pkg_dir.parent
