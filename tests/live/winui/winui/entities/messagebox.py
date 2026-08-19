"""模态消息框（实体层）：标题 + 多行消息 + 按钮组。

通过 Application.show_messagebox() 弹出，打开期间独占输入；
Esc 关闭并回调 -1，按钮点击回调对应序号。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from winui.adapters.buffer import text_width
from winui.entities.theme import Theme
from winui.entities.widgets import Button, Container, Label

if TYPE_CHECKING:
    from winui.adapters.buffer import CharBuffer
    from winui.app import Application
    from winui.entities.events import KeyEvent, MouseEvent

logger = logging.getLogger("winui.messagebox")


class MessageBox(Container):
    """模态对话框。on_close(index) 携带被点击按钮的序号（Esc 为 -1）。

    宿主 Application 在 push_modal 时通过 _attach 注入，关闭时自动移除模态。
    """

    def __init__(self, title: str, message: str,
                 buttons: list[str] | None = None,
                 on_close: Callable[[int], None] | None = None) -> None:
        super().__init__(focusable=True)
        self.title = title
        self.lines = message.split("\n")
        self.buttons = buttons or ["确定"]
        self.on_close = on_close
        self._host: Application | None = None
        self._build()

    def _attach(self, host: "Application") -> None:
        self._host = host

    def _build(self) -> None:
        # 宽度按显示宽度计算（东亚宽字符占 2 列），避免中文溢出边框
        msg_w = max((text_width(line) for line in self.lines), default=0)
        btn_w = sum(text_width(b) + 4 for b in self.buttons) + 2 * max(len(self.buttons) - 1, 0)
        inner_w = max(msg_w, btn_w, text_width(self.title), 20)
        self.width = inner_w + 4
        self.height = len(self.lines) + 5

        self._title_label = Label(self.title, style_name="title") if self.title else None
        if self._title_label:
            self.add(self._title_label)
        self._body = Container()
        for line in self.lines:
            self._body.add(Label(line, width=inner_w, height=1))
        self.add(self._body)
        self._btn_row = Container(height=1)
        self._buttons: list[Button] = []
        for i, label in enumerate(self.buttons):
            btn = Button(label, on_click=lambda b, i=i: self._close(i))
            btn.width = text_width(label) + 4
            self._btn_row.add(btn)
            self._buttons.append(btn)
        self.add(self._btn_row)

    def arrange(self, x: int, y: int, w: int, h: int) -> None:
        """精确手动布局：标题 / 消息 / 底部居中按钮行。"""
        self.set_rect(x, y, w, h)
        line_y = y + 1
        if self._title_label:
            self._title_label.set_rect(x + 1, line_y, w - 2, 1)
            line_y += 1
        self._body.set_rect(x + 2, line_y, w - 4, len(self.lines))
        for i, child in enumerate(self._body.children):
            child.set_rect(x + 2, line_y + i, w - 4, 1)
        btn_w = sum(b.width for b in self._buttons) + 2 * max(len(self._buttons) - 1, 0)
        bx = x + max(0, (w - btn_w) // 2)
        by = y + h - 2
        # 按钮行容器位于边框内侧，避免 fill 背景盖掉左右边框竖线
        self._btn_row.set_rect(x + 1, by, w - 2, 1)
        for _i, btn in enumerate(self._buttons):
            bw = btn.width
            btn.set_rect(bx, by, bw, 1)
            bx += bw + 2

    def center_on(self, app_w: int, app_h: int) -> None:
        """弹层前把对话框居中（Application.push_modal 时调用）。"""
        x = max(0, (app_w - self.width) // 2)
        y = max(0, (app_h - self.height) // 2)
        self.arrange(x, y, self.width, self.height)

    def _close(self, index: int) -> None:
        """按按钮序号关闭：移除模态并回调 on_close（Esc 为 -1）。"""
        if self._host is not None:
            self._host.pop_modal()
        logger.info("消息框关闭 result=%d", index)
        if self.on_close:
            self.on_close(index)

    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        """对话框背景与边框由自身绘制，子级内容随后叠加。"""
        attr = theme.resolve(theme.default)
        border = theme.resolve(theme.border)
        buf.fill(self.x, self.y, self.w, self.h, " ", attr)
        for xx in range(self.w):
            buf.put_char(self.x + xx, self.y, "─", border)
            buf.put_char(self.x + xx, self.y + self.h - 1, "─", border)
        for yy in range(1, self.h - 1):
            buf.put_char(self.x, self.y + yy, "│", border)
            buf.put_char(self.x + self.w - 1, self.y + yy, "│", border)
        buf.put_char(self.x, self.y, "┌", border)
        buf.put_char(self.x + self.w - 1, self.y, "┐", border)
        buf.put_char(self.x, self.y + self.h - 1, "└", border)
        buf.put_char(self.x + self.w - 1, self.y + self.h - 1, "┘", border)
        for child in self.children:
            if child.visible:
                child.draw(buf, theme)

    # ---- 事件 ----
    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        if ev.name == "esc":
            self._close(-1)
            return True
        if ev.name in ("left", "right"):
            current = next((i for i, b in enumerate(self._buttons) if b.focused), 0)
            d = 1 if ev.name == "right" else -1
            app.focus(self._buttons[(current + d) % len(self._buttons)])
            return True
        if ev.name == "tab":
            current = next((i for i, b in enumerate(self._buttons) if b.focused), 0)
            d = 1 if not ev.shift else -1
            app.focus(self._buttons[(current + d) % len(self._buttons)])
            return True
        # Enter/空格等分发给当前聚焦的按钮（Container.handle_key）
        return super().handle_key(ev, app)

    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        if not self.contains(ev.x, ev.y):
            return False
        return super().handle_mouse(ev, app)