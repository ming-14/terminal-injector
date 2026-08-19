"""剪贴板适配层（接口适配层）。

实体层的文本控件通过本模块读写系统剪贴板，
避免实体层直接依赖框架驱动实现。
"""

from winui.drivers.clipboard import get_text, set_text

__all__ = ["get_text", "set_text"]