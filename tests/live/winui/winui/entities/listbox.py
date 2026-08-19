"""列表控件（实体层）：ListBox 支持单选/多选、滚动、鼠标与滚轮。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from winui.entities.theme import Theme
from winui.entities.widgets import Widget

if TYPE_CHECKING:
    from winui.adapters.buffer import CharBuffer
    from winui.app import Application
    from winui.entities.events import KeyEvent, MouseEvent

logger = logging.getLogger("winui.widgets")


class ListBox(Widget):
    """可滚动列表。Enter 触发 on_select；多选时 Space 切换勾选。"""

    def __init__(self, items: list[str] | None = None, *, selected: int = -1,
                 multi: bool = False,
                 on_select: Callable[[ListBox, int, str], None] | None = None,
                 on_change: Callable[[ListBox, int], None] | None = None,
                 **kw) -> None:
        super().__init__(focusable=True, **kw)
        self.items: list[str] = list(items or [])
        self.multi = multi
        self.selected = selected          # 当前行（焦点行）
        self.multi_sel: set[int] = set()  # 多选集合
        self.viewport = 0                 # 顶部可见行
        self.on_select = on_select
        self.on_change = on_change

    # ---- 数据 ----
    def set_items(self, items: list[str]) -> None:
        self.items = list(items)
        if self.selected >= len(self.items):
            self.selected = len(self.items) - 1
        self.multi_sel = {i for i in self.multi_sel if i < len(self.items)}
        self._clamp_viewport()

    def _visible_h(self) -> int:
        return max(1, self.h)

    def _clamp_viewport(self) -> None:
        vh = self._visible_h()
        if self.selected >= 0:
            if self.selected < self.viewport:
                self.viewport = self.selected
            elif self.selected >= self.viewport + vh:
                self.viewport = self.selected - vh + 1
        self.viewport = max(0, min(self.viewport, max(0, len(self.items) - vh)))

    # ---- 事件 ----
    def _step(self, dy: int) -> None:
        if not self.items:
            return
        self.selected = max(0, min(self.selected + dy, len(self.items) - 1))
        self._clamp_viewport()
        if self.on_change:
            self.on_change(self, self.selected)

    def _trigger(self) -> None:
        if 0 <= self.selected < len(self.items) and self.on_select:
            self.on_select(self, self.selected, self.items[self.selected])

    def _toggle_multi(self) -> None:
        if not self.multi or self.selected < 0:
            return
        if self.selected in self.multi_sel:
            self.multi_sel.discard(self.selected)
        else:
            self.multi_sel.add(self.selected)
        if self.on_change:
            self.on_change(self, self.selected)

    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        name = ev.name
        if name in ("up", "down"):
            direction = -1 if name == "up" else 1
            if ev.ctrl:
                self.selected = 0 if direction < 0 else len(self.items) - 1
                self._clamp_viewport()
                if self.on_change:
                    self.on_change(self, self.selected)
            else:
                self._step(direction)
            return True
        if name in ("pgup", "pgdn"):
            self._step(-(self._visible_h() - 1) if name == "pgup" else self._visible_h() - 1)
            return True
        if name == "home":
            self.selected = 0
            self._clamp_viewport()
            return True
        if name == "end":
            self.selected = len(self.items) - 1
            self._clamp_viewport()
            return True
        if name == "enter":
            self._trigger()
            return True
        if name == "space" and self.multi:
            self._toggle_multi()
            return True
        return False

    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        if ev.kind == "click":
            app.focus(self)
            rel = ev.y - self.y
            if 0 <= rel < self._visible_h():
                idx = self.viewport + rel
                if idx < len(self.items):
                    self.selected = idx
                    if self.multi and ev.ctrl:
                        self._toggle_multi()
                    self._clamp_viewport()
                    if self.on_change:
                        self.on_change(self, self.selected)
            return True
        if ev.kind == "wheel":
            self.viewport = max(0, min(max(0, len(self.items) - self._visible_h()),
                                       self.viewport - ev.delta * 3))
            return True
        return False

    # ---- 绘制 ----
    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        attr = theme.resolve(theme.default)
        buf.fill(self.x, self.y, self.w, self.h, " ", attr)
        vh = self._visible_h()
        for row in range(vh):
            idx = self.viewport + row
            if idx >= len(self.items):
                break
            text = self.items[idx]
            style = theme.default
            if idx == self.selected:
                style = theme.focus if self.focused else theme.selected
            if self.multi and idx in self.multi_sel:
                prefix = "[*] "
            else:
                prefix = "    " if self.multi else "  "
            line = prefix + text
            buf.put_text(self.x, self.y + row, line[: self.w], theme.resolve(style))
        # 滚动条
        if len(self.items) > vh:
            thumb = max(1, round(vh * vh / len(self.items)))
            off = round(self.viewport * (vh - thumb) / (len(self.items) - vh))
            sx = self.x + self.w - 1
            for row in range(vh):
                on = off <= row < off + thumb
                buf.put_char(sx, self.y + row, "█" if on else "░",
                             theme.resolve(theme.scrollbar))