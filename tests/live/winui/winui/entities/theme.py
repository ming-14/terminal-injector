"""主题与颜色模型（实体层）。

颜色按 Windows 控制台 16 色调色板建模；Style 描述前景/背景/修饰，
Theme.resolve 将其换算为 win32 属性值，供适配层/驱动层使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from functools import lru_cache

# ---- Win32 控制台属性位（供 buffer/console 层引用） ----
FG_BLUE = 0x0001
FG_GREEN = 0x0002
FG_RED = 0x0004
FG_INTENSITY = 0x0008
BG_BLUE = 0x0010
BG_GREEN = 0x0020
BG_RED = 0x0040
BG_INTENSITY = 0x0080
COMMON_LVB_LEADING_BYTE = 0x0100
COMMON_LVB_TRAILING_BYTE = 0x0200
COMMON_LVB_REVERSE_VIDEO = 0x4000
COMMON_LVB_UNDERSCORE = 0x8000


class Color(IntEnum):
    """16 色调色板，与 Windows 控制台一致。"""
    BLACK = 0
    DARK_BLUE = 1
    DARK_GREEN = 2
    DARK_CYAN = 3
    DARK_RED = 4
    DARK_MAGENTA = 5
    DARK_YELLOW = 6
    GRAY = 7
    BRIGHT_BLACK = 8
    BLUE = 9
    GREEN = 10
    CYAN = 11
    RED = 12
    MAGENTA = 13
    YELLOW = 14
    WHITE = 15


@dataclass(frozen=True, slots=True)
class Style:
    """文本样式：前景/背景色与修饰位。"""
    fg: Color = Color.GRAY
    bg: Color = Color.BLACK
    bold: bool = False
    reverse: bool = False
    underline: bool = False


class Theme:
    """TUI 主题：为控件角色提供配色。通过 resolve() 换算 win32 属性。"""

    def __init__(
        self,
        *,
        default: Style = Style(Color.GRAY, Color.BLACK),
        title: Style = Style(Color.BLACK, Color.WHITE),
        focus: Style = Style(Color.BLACK, Color.GRAY),
        selected: Style = Style(Color.BLACK, Color.CYAN),
        border: Style = Style(Color.DARK_CYAN, Color.BLACK),
        disabled: Style = Style(Color.BRIGHT_BLACK, Color.BLACK),
        accent: Style = Style(Color.CYAN, Color.BLACK),
        warning: Style = Style(Color.YELLOW, Color.BLACK),
        error: Style = Style(Color.RED, Color.BLACK),
        success: Style = Style(Color.GREEN, Color.BLACK),
        input: Style = Style(Color.WHITE, Color.BLACK),
        scrollbar: Style = Style(Color.BRIGHT_BLACK, Color.BLACK),
        dim: Style = Style(Color.BRIGHT_BLACK, Color.BLACK),
    ) -> None:
        self.default = default
        self.title = title
        self.focus = focus
        self.selected = selected
        self.border = border
        self.disabled = disabled
        self.accent = accent
        self.warning = warning
        self.error = error
        self.success = success
        self.input = input
        self.scrollbar = scrollbar
        self.dim = dim

    @lru_cache(maxsize=256)
    def resolve(self, style: Style) -> int:
        """把 Style 换算为 Win32 属性值。"""
        fg, bg = style.fg, style.bg
        if style.reverse:
            fg, bg = bg, fg
        attr = int(fg) | (int(bg) << 4)
        if style.bold:
            attr |= FG_INTENSITY
        if style.underline:
            attr |= COMMON_LVB_UNDERSCORE
        return attr


DEFAULT_THEME = Theme()