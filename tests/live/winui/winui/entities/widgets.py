"""控件基类与基础控件（实体层）。

Widget 是 TUI 的最小绘制/交互单元；Container 承载子级并负责事件分派与焦点环。
所有控件都不持有驱动/适配层依赖，绘制目标为 CharBuffer，事件为实体层事件。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from winui.adapters.buffer import clip_text, text_width
from winui.entities.layout import FillLayout, Layout
from winui.entities.theme import Theme

if TYPE_CHECKING:
    from winui.adapters.buffer import CharBuffer
    from winui.app import Application
    from winui.entities.events import KeyEvent, MouseEvent

logger = logging.getLogger("winui.widgets")


class Widget:
    """所有控件的基类。

    x/y/w/h 在布局后被 set_rect() 确定；width/height 保留布局前的尺寸提示
    （None 表示由布局器按权重分配）。
    """

    def __init__(self, *, width: int | None = None, height: int | None = None,
                 visible: bool = True, enabled: bool = True,
                 focusable: bool = False) -> None:
        self.x = 0
        self.y = 0
        self.w = 0
        self.h = 0
        self.width = width
        self.height = height
        self.visible = visible
        self.enabled = enabled
        self.focusable = focusable
        self.focused = False
        self.parent: Container | None = None
        # GridLayout 定位属性
        self.grid_row = 0
        self.grid_col = 0
        self.grid_rowspan = 1
        self.grid_colspan = 1

    # ---- 布局 ----
    def set_rect(self, x: int, y: int, w: int, h: int) -> None:
        self.x, self.y, self.w, self.h = int(x), int(y), int(w), int(h)

    def contains(self, px: int, py: int) -> bool:
        return (self.visible and self.x <= px < self.x + self.w
                and self.y <= py < self.y + self.h)

    # ---- 绘制 ----
    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        buf.fill(self.x, self.y, self.w, self.h, " ", theme.resolve(theme.default))

    # ---- 事件（返回 True 表示已消费） ----
    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        return False

    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        return False

    def on_focus(self, app: Application) -> None:
        self.focused = True
        logger.debug("焦点进入 %s", self)

    def on_blur(self, app: Application) -> None:
        self.focused = False

    def __repr__(self) -> str:
        return f"<{type(self).__name__} @{self.x},{self.y} {self.w}x{self.h}>"


class Container(Widget):
    """可容纳子级控件的容器，负责递归绘制与事件分派。"""

    def __init__(self, *, layout: Layout | None = None, **kw) -> None:
        super().__init__(**kw)
        self.layout = layout or FillLayout()
        self.children: list[Widget] = []

    def add(self, child: Widget) -> Container:
        child.parent = self
        self.children.append(child)
        return self

    # ---- 布局与绘制 ----
    def arrange(self, x: int, y: int, w: int, h: int) -> None:
        self.set_rect(x, y, w, h)
        self.layout.arrange(self.x, self.y, self.w, self.h, self.children)
        for child in self.children:
            if isinstance(child, Container):
                child.arrange(child.x, child.y, child.w, child.h)

    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        super().draw(buf, theme)
        for child in self.children:
            if child.visible:
                child.draw(buf, theme)

    # ---- 焦点环 ----
    def focusables(self) -> list[Widget]:
        """深度优先收集可聚焦子级。"""
        result: list[Widget] = []
        for child in self.children:
            if not child.visible:
                continue
            if isinstance(child, Container):
                result.extend(child.focusables())
            elif child.focusable and child.enabled:
                result.append(child)
        return result

    # ---- 事件分派 ----
    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        # 焦点子级优先
        for child in self.children:
            if child.visible and child.focused:
                if isinstance(child, Container):
                    if child.handle_key(ev, app):
                        return True
                elif child.handle_key(ev, app):
                    return True
                break
        return False

    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        # 逆序：后添加的控件层级在上，优先响应
        for child in reversed(self.children):
            if not child.visible or not child.contains(ev.x, ev.y):
                continue
            if isinstance(child, Container):
                if child.handle_mouse(ev, app):
                    return True
            elif child.handle_mouse(ev, app):
                return True
            else:
                # 未消费的点击：命中可聚焦控件则夺焦
                if ev.kind == "click" and child.focusable and child.enabled:
                    app.focus(child)
                if ev.kind == "click":
                    return True
        return False


class Label(Widget):
    """静态文本标签。"""

    def __init__(self, text: str = "", *, align: str = "left",
                 style_name: str = "default", **kw) -> None:
        super().__init__(**kw)
        self.text = text
        self.align = align  # left / center / right
        self.style_name = style_name

    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        style = getattr(theme, self.style_name)
        buf.fill(self.x, self.y, self.w, self.h, " ", theme.resolve(style))
        line = self.text
        w = text_width(line)
        if self.align == "center":
            line = " " * max(0, (self.w - w) // 2) + line
        elif self.align == "right":
            line = " " * max(0, self.w - w) + line
        buf.put_text(self.x, self.y, line, theme.resolve(style))

    def __repr__(self) -> str:
        return f"<Label {self.text!r} @{self.x},{self.y}>"


class Button(Widget):
    """按钮：聚焦时 Enter/空格触发，未聚焦时点击触发。"""

    def __init__(self, text: str = "", *,
                 on_click: Callable[[Button], None] | None = None, **kw) -> None:
        super().__init__(focusable=True, **kw)
        self.text = text
        self.on_click = on_click

    def _label(self) -> str:
        return f"< {self.text} >"

    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        if not self.enabled:
            style = theme.disabled
        elif self.focused:
            style = theme.focus
        else:
            style = theme.default
        attr = theme.resolve(style)
        buf.fill(self.x, self.y, self.w, self.h, " ", attr)
        label = self._label()
        buf.put_text(self.x + max(0, (self.w - text_width(label)) // 2),
                     self.y, label, attr)

    def _activate(self, app: Application) -> None:
        logger.info("按钮触发 %r", self.text)
        if self.on_click:
            self.on_click(self)

    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        if ev.name in ("enter", "space"):
            if ev.name == "space" and not ev.ch:
                return False
            self._activate(app)
            return True
        return False

    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        if ev.kind == "click":
            app.focus(self)
            self._activate(app)
            return True
        return False


class CheckBox(Widget):
    """复选框：聚焦时 Space/Enter/点击切换。"""

    def __init__(self, text: str = "", *, checked: bool = False,
                 on_change: Callable[[CheckBox, bool], None] | None = None, **kw) -> None:
        super().__init__(focusable=True, **kw)
        self.text = text
        self.checked = checked
        self.on_change = on_change

    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        style = theme.default if not self.focused else theme.focus
        attr = theme.resolve(style)
        buf.fill(self.x, self.y, self.w, self.h, " ", attr)
        marker = "[x]" if self.checked else "[ ]"
        buf.put_text(self.x, self.y, f"{marker} {self.text}", attr)

    def toggle(self, app: Application) -> None:
        self.checked = not self.checked
        logger.info("复选框 %r -> %s", self.text, self.checked)
        if self.on_change:
            self.on_change(self, self.checked)

    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        if ev.name in ("enter", "space"):
            self.toggle(app)
            return True
        return False

    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        if ev.kind == "click":
            app.focus(self)
            self.toggle(app)
            return True
        return False


class ProgressBar(Widget):
    """进度条：value 为 0.0~1.0，绘制百分比与填充块。"""

    def __init__(self, value: float = 0.0, *, text: str = "", **kw) -> None:
        super().__init__(**kw)
        self.value = value
        self.text = text

    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        attr = theme.resolve(theme.default)
        buf.fill(self.x, self.y, self.w, self.h, " ", attr)
        ratio = max(0.0, min(1.0, self.value))
        fill_w = int(self.w * ratio)
        filled = "█" * fill_w
        empty = "░" * (self.w - fill_w)
        buf.put_text(self.x, self.y, filled + empty, attr)
        label = self.text or f"{ratio * 100:.0f}%"
        if self.w >= text_width(label) + 2:
            buf.put_text(self.x + max(0, (self.w - text_width(label)) // 2),
                         self.y, label, theme.resolve(theme.accent))


class StatusBar(Widget):
    """状态栏：左/右两段文本，常用于展示提示与快捷键。"""

    def __init__(self, *, left: str = "", right: str = "") -> None:
        super().__init__(height=1)
        self.left = left
        self.right = right

    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        attr = theme.resolve(theme.title)
        buf.fill(self.x, self.y, self.w, self.h, " ", attr)
        buf.put_text(self.x + 1, self.y, clip_text(self.left, self.w - 2), attr)
        right = clip_text(self.right, max(0, self.w - 4))
        buf.put_text(self.x + self.w - 1 - text_width(right), self.y, right, attr)