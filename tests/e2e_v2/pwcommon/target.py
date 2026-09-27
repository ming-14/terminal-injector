"""目标脚本管理（内嵌目标脚本 → _targets/<name>.py）。

前导（ctypes Console API 绑定 + rec/check/done）**单一来源**取 v1 的
`tests/e2e/common/target.py` 里的 `TARGET_PREAMBLE` 字面量，避免两份拷贝漂移。

为什么用 ast 取字面量而不是 import：v1 那个模块用相对导入（`from . import paths`），
按文件路径加载会直接 ImportError；而且 v2 不该为了拿一段文本把 v1 的包塞进 sys.path
（那正是同名包互相顶掉的温床）。
"""
import ast
import os

from . import paths

_PREAMBLE_CACHE = None

V1_TARGET_MODULE = os.path.join(paths.PROJECT_ROOT, "tests", "e2e", "common", "target.py")


def preamble() -> str:
    """返回 v1 的 TARGET_PREAMBLE 原文（缓存）。"""
    global _PREAMBLE_CACHE
    if _PREAMBLE_CACHE is not None:
        return _PREAMBLE_CACHE
    with open(V1_TARGET_MODULE, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), V1_TARGET_MODULE)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "TARGET_PREAMBLE":
                    _PREAMBLE_CACHE = ast.literal_eval(node.value)
                    return _PREAMBLE_CACHE
    raise RuntimeError("在 {} 里找不到 TARGET_PREAMBLE".format(V1_TARGET_MODULE))


def write_target(name: str, body: str) -> str:
    """生成 _targets/<name>.py（前导 + 正文），返回绝对路径。"""
    paths.ensure_dirs()
    path = os.path.join(paths.TARGETS_DIR, name + ".py")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(preamble())
        f.write("\n")
        f.write(body)
    return path
