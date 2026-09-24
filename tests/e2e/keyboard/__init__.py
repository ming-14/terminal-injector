# 本文件必须存在，不要删除。
#
# 原因：PyPI 上有同名第三方包 `keyboard`（全局键盘钩子库）。若本目录没有
# `__init__.py`，它会被解释成**命名空间包**，而命名空间包的优先级低于
# site-packages 里的常规包 ⇒ `import keyboard` 解析到第三方库，
# `from keyboard._common import ...` 直接 ModuleNotFoundError（实测：9 个
# keyboard 用例在导入期即失败，根本没跑到注入逻辑）。
# 加上本文件后本目录成为常规包，优先于第三方同名包。
