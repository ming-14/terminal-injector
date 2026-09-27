"""pywezterm 引导与"不可用即 UNSUPPORTED"协议。

测试文件统一这么用：

    from pwcommon import pyterm
    ...
    def run():
        pywezterm = pyterm.require()      # 不可用时打印 SUMMARY: UNSUPPORTED 并退出 0
        ...

`require()` 内部 sys.exit(0)——调用方是 `sys.exit(run())`，所以 UNSUPPORTED
自然按「不算失败」的退出码收场（与 v1 约定一致）。
"""
import sys

from . import paths

_module = None


def load():
    """导入并缓存 pywezterm；失败抛 ImportError。"""
    global _module
    if _module is not None:
        return _module
    d = paths.pywezterm_dir()
    if d not in sys.path:
        sys.path.insert(0, d)
    import pywezterm  # noqa: E402  (需先改 sys.path)
    _module = pywezterm
    return _module


def available() -> bool:
    try:
        load()
        return True
    except ImportError:
        return False


def require():
    """返回 pywezterm 模块；不可用时按 UNSUPPORTED 收场（不返回）。"""
    try:
        return load()
    except ImportError as e:
        print("  [SKIP] 无法 import pywezterm (PWTERM_DIR={!r}): {}".format(
            paths.pywezterm_dir(), e))
        print("\nSUMMARY: UNSUPPORTED (pywezterm 不可用)")
        sys.exit(0)


def unsupported(reason: str):
    """以 UNSUPPORTED 收场（缺前提依赖时用，例如未装 textual / 找不到 vim）。"""
    print("  [SKIP] {}".format(reason))
    print("\nSUMMARY: UNSUPPORTED ({})".format(reason))
    sys.exit(0)
