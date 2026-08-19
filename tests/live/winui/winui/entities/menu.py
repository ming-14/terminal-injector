"""菜单栏（实体层）：MenuBar 顶部菜单 + 下拉 MenuPopup 弹层。

弹层交互经 Application 的模态栈（push_modal/pop_modal）接管全部输入，
绘制在容器内容之上。Alt+字母 / 点击打开，方向键导航，Enter 触发，Esc 关闭。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from winui.adapters.buffer import text_width
from winui.entities.theme import Theme
from winui.entities.widgets import Container, Widget

if TYPE_CHECKING:
    from winui.adapters.buffer import CharBuffer
    from winui.app import Application
    from winui.entities.events import KeyEvent, MouseEvent

logger = logging.getLogger("winui.menu")


class MenuItem:
    """单个菜单命令。label 含 & 前缀时，Alt+该字母可直接打开所在菜单。"""
    def __init__(self, label: str, callback: Callable[[], None] | None = None,
                 *, separator: bool = False) -> None:
        self.label = label
        self.callback = callback
        self.separator = separator

    @property
    def hotkey(self) -> str | None:
        if "&" in self.label:
            idx = self.label.index("&")
            return self.label[idx + 1].lower()
        return None


class Menu:
    """一个菜单（含标题与条目）。"""
    def __init__(self, label: str, items: list[MenuItem]) -> None:
        self.label = label
        self.items = items

    @property
    def hotkey(self) -> str | None:
        if "&" in self.label:
            idx = self.label.index("&")
            return self.label[idx + 1].lower()
        return None


class MenuPopup(Widget):
    """下拉菜单弹层：自身为模态输入消费者。构造后由 open_menu 定位（set_rect）。"""

    def __init__(self, bar: "MenuBar", menu: Menu) -> None:
        super().__init__(width=20, height=1)
        self.bar = bar
        self.menu = menu
        self.cursor = 0

    def _content_h(self) -> int:
        return len(self.menu.items)

    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        # 计算合适的弹层尺寸（不超出屏幕右/下边界）；高 = 菜单项数 + 上下边框
        max_width = max((text_width(i.label)
                         for i in self.menu.items if not i.separator),
                        default=8) + 2
        w = min(max_width, buf.w - self.x)
        h = min(self._content_h() + 2, buf.h - self.y)
        self.set_rect(self.x, self.y, w, h)

        attr = theme.resolve(theme.default)
        border = theme.resolve(theme.border)
        buf.fill(self.x, self.y, w, h, " ", attr)
        for xx in range(w):
            buf.put_char(self.x + xx, self.y, "─", border)
            buf.put_char(self.x + xx, self.y + h - 1, "─", border)
        for yy in range(1, h - 1):
            buf.put_char(self.x, yy + self.y, "│", border)
            buf.put_char(self.x + w - 1, yy + self.y, "│", border)
        buf.put_char(self.x, self.y, "┌", border)
        buf.put_char(self.x + w - 1, self.y, "┐", border)
        buf.put_char(self.x, self.y + h - 1, "└", border)
        buf.put_char(self.x + w - 1, self.y + h - 1, "┘", border)

        row = 0
        for i, item in enumerate(self.menu.items):
            if row + 1 >= h - 1:
                break
            if item.separator:
                buf.fill(self.x + 1, self.y + row + 1, w - 2, 1, "─", theme.resolve(theme.dim))
                row += 1
                continue
            style = theme.focus if i == self.cursor else theme.default
            text = item.label.replace("&", "")
            buf.put_text(self.x + 1, self.y + row + 1, text[: w - 2], theme.resolve(style))
            row += 1

    # ---- 输入 ----
    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        name = ev.name
        items = [i for i in self.menu.items if not i.separator]
        if name in ("up", "down"):
            d = -1 if name == "up" else 1
            self.cursor = (self.cursor + d) % max(1, len(items))
            return True
        if name == "enter":
            item = items[self.cursor] if self.cursor < len(items) else None
            app.pop_modal()
            self.bar.close(app)
            if item and item.callback:
                item.callback()
            return True
        if name == "esc":
            app.pop_modal()
            self.bar.close(app)
            return True
        if name == "left" or name == "right":
            d = -1 if name == "left" else 1
            app.pop_modal()
            self.bar.open_menu(app, self.bar.menus.index(self.menu) + d)
            return True
        return False

    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        if not self.contains(ev.x, ev.y):
            # 点击弹层外部：菜单栏区域切换菜单，其他区域关闭弹层
            if ev.kind == "click":
                if self.bar.handle_outside_click(ev, app):
                    return True
                app.pop_modal()
                self.bar.close(app)
                return True
            return False
        if ev.kind == "wheel":
            return False
        if ev.kind == "click":
            rel_y = ev.y - self.y
            items = [i for i in self.menu.items if not i.separator]
            idx = rel_y - 1
            if 0 <= idx < len(items):
                item = items[idx]
                self.cursor = idx
                app.pop_modal()
                self.bar.close(app)
                if item.callback:
                    item.callback()
            return True
        return False


class MenuBar(Container):
    """顶部菜单栏。作为根容器首个子级使用。"""

    def __init__(self, menus: list[Menu] | None = None) -> None:
        super().__init__(height=1)
        self.menus: list[Menu] = list(menus or [])
        self.open_index: int | None = None

    def add_menu(self, menu: Menu) -> MenuBar:
        self.menus.append(menu)
        return self

    # ---- 打开/关闭 ----
    def open_menu(self, app: Application, index: int) -> None:
        if not self.menus:
            return
        index %= len(self.menus)
        self.open_index = index
        menu = self.menus[index]
        popup = MenuPopup(self, menu)
        popup.set_rect(self._menu_x(index), self.y + 1, 20, 1)
        app.push_modal(popup)
        logger.info("打开菜单 %r", menu.label)

    def close(self, app: Application) -> None:
        self.open_index = None

    def toggle_menu(self, app: Application, index: int) -> None:
        if self.open_index == index:
            app.pop_modal()
            self.close(app)
        else:
            if self.open_index is not None:
                app.pop_modal()
            self.open_menu(app, index)

    def _menu_x(self, index: int) -> int:
        x = 0
        for i, m in enumerate(self.menus):
            if i == index:
                return x
            x += len(m.label) + 2
        return x

    # ---- 鼠标 ----
    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        if ev.kind == "click" and self.open_index is None:
            x = ev.x - self.x
            acc = 0
            for i, m in enumerate(self.menus):
                w = len(m.label) + 2
                if acc <= x < acc + w:
                    self.toggle_menu(app, i)
                    return True
                acc += w
        return False

    def handle_outside_click(self, ev: MouseEvent, app: Application) -> bool:
        """弹层打开时点击菜单栏内其他菜单名：切换菜单。"""
        if ev.y == self.y:
            x = ev.x - self.x
            acc = 0
            for i, m in enumerate(self.menus):
                w = len(m.label) + 2
                if acc <= x < acc + w:
                    app.pop_modal()
                    if self.open_index == i:
                        self.close(app)
                    else:
                        self.open_menu(app, i)
                    return True
                acc += w
        return False

    # ---- 键盘 ----
    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        if self.open_index is not None:
            return False
        if ev.alt and (ev.ch is None or ev.vk == 0x12):
            # 单独 Alt（VK_MENU）：打开第一个菜单
            if self.menus:
                self.open_menu(app, 0)
                return True
        if ev.alt and ev.ch and ev.ch.isalpha():
            ch = ev.ch.lower()
            for i, m in enumerate(self.menus):
                if m.hotkey == ch:
                    self.toggle_menu(app, i)
                    return True
        return False

    # ---- 绘制 ----
    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        attr = theme.resolve(theme.title)
        buf.fill(self.x, self.y, self.w, self.h, " ", attr)
        x = self.x
        for i, m in enumerate(self.menus):
            style = theme.focus if i == self.open_index else theme.title
            label = f" {m.label.replace('&', '')} "
            buf.put_text(x, self.y, label, theme.resolve(style))
            x += len(label)
        # 分隔线右侧留白（放快捷键提示等）