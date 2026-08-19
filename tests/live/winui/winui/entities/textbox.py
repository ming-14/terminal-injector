"""文本输入控件（实体层）：单行 TextBox 与多行 TextArea。

共同特性：
  - 光标按“显示列”移动（东亚宽字符占 2 列，光标不落入尾格）
  - Shift+方向键选择文本，Ctrl+A 全选，Ctrl+C/X/V 操作系统剪贴板
  - 水平/垂直自动滚动保证光标可见
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from winui.adapters.buffer import char_width
from winui.adapters.clipboard import get_text, set_text
from winui.entities.theme import Theme
from winui.entities.widgets import Widget

if TYPE_CHECKING:
    from winui.adapters.buffer import CharBuffer
    from winui.app import Application
    from winui.entities.events import KeyEvent, MouseEvent

logger = logging.getLogger("winui.text")


# ---------------------------------------------------------------- 列/索引换算

def col_to_index(line: str, col: int) -> int:
    """显示列 → 字符索引；col 越界时钳制到最近合法值。"""
    if col <= 0:
        return 0
    total = 0
    for i, ch in enumerate(line):
        w = char_width(ch)
        if col < total + w:
            return i
        total += w
        if total >= col:
            return i + 1
    return len(line)


def index_to_col(line: str, idx: int) -> int:
    """字符索引 → 显示列。"""
    return sum(char_width(ch) for ch in line[:idx])


def line_width(line: str) -> int:
    return sum(char_width(ch) for ch in line)


def char_at(line: str, col: int) -> str | None:
    """返回 col 处的整字符（宽字符返回其首字）；越界返回 None。"""
    idx = col_to_index(line, col)
    if idx >= len(line):
        return None
    return line[idx]


def snap_left(line: str, col: int) -> int:
    """光标左移一列后的位置：若落入宽字符尾格则吸附到该字符首格。

    已在字符边界的列保持不变。
    """
    if col <= 0:
        return 0
    idx = col_to_index(line, col)
    start = index_to_col(line, idx)
    return start if idx < len(line) and start != col else col


def backspace_at(line: str, col: int) -> tuple[str, int] | None:
    """删除光标前的字符（宽字符整体删除），返回 (新行, 新光标列)。

    光标落在宽字符尾格内时删除该宽字符自身；无可删字符返回 None。
    """
    if col <= 0:
        return None
    idx = col_to_index(line, col)
    if idx == 0:
        return None
    if idx < len(line) and col > index_to_col(line, idx):
        del_idx = idx          # 光标在字符中部（宽字符尾格）：删该字符
    else:
        del_idx = idx - 1      # 光标在边界：删前一个字符
    new = line[:del_idx] + line[del_idx + 1:]
    return new, index_to_col(new, del_idx)


# ---------------------------------------------------------------- 选择模型

class Selection:
    """文本选择区（按显示列）。anchor 固定端，cursor 活动端。"""

    def __init__(self) -> None:
        self.anchor: int | None = None
        self.cursor: int | None = None

    def start_selection(self, col: int) -> None:
        self.anchor = col
        self.cursor = col

    def extend(self, col: int) -> None:
        self.cursor = col

    def clear(self) -> None:
        self.anchor = None
        self.cursor = None

    @property
    def active(self) -> bool:
        return self.anchor is not None and self.anchor != self.cursor

    def range(self) -> tuple[int, int]:
        if not self.active:
            return 0, 0
        return min(self.anchor, self.cursor), max(self.anchor, self.cursor)

    def selected_text(self, line: str) -> str:
        a, b = self.range()
        ia, ib = col_to_index(line, a), col_to_index(line, b)
        return line[ia:ib]


# ---------------------------------------------------------------- TextBox

class TextBox(Widget):
    """单行文本输入框。Enter 触发 on_submit。"""

    def __init__(self, value: str = "", *, max_len: int | None = None,
                 on_submit: Callable[[TextBox, str], None] | None = None,
                 on_change: Callable[[str], None] | None = None, **kw) -> None:
        super().__init__(focusable=True, height=1, **kw)
        self.value = value
        self.max_len = max_len
        self.on_submit = on_submit
        self.on_change = on_change
        self.cursor = 0          # 显示列
        self.viewport = 0        # 水平滚动偏移
        self.sel = Selection()

    def set_value(self, value: str) -> None:
        self.value = value
        self.cursor = 0
        self.sel.clear()
        if self.on_change:
            self.on_change(value)

    def _visible_width(self) -> int:
        return max(1, self.w - 2)

    def _ensure_visible(self) -> None:
        """滚动视口使光标可见。"""
        vw = self._visible_width()
        if self.cursor < self.viewport:
            self.viewport = self.cursor
        elif self.cursor >= self.viewport + vw:
            self.viewport = self.cursor - vw + 1

    def _move(self, dx: int, select: bool, *, jump: bool = False) -> None:
        if select and not self.sel.active:
            self.sel.start_selection(self.cursor)
        if jump:  # Home/End
            target = 0 if dx < 0 else line_width(self.value)
        else:
            target = self.cursor + dx
            if dx > 0:
                # 向右：光标落进宽字符尾格时直接跳过该字符
                idx = col_to_index(self.value, target)
                start = index_to_col(self.value, idx)
                if idx < len(self.value) and start != target:
                    target = start + char_width(self.value[idx])
            else:
                # 向左：吸附到字符边界
                target = snap_left(self.value, target)
            target = max(0, min(target, line_width(self.value)))
        self.cursor = target
        if select:
            self.sel.extend(self.cursor)
        else:
            self.sel.clear()
        self._ensure_visible()

    def _delete_range(self) -> bool:
        """删除选区；有选区时返回 True。"""
        if not self.sel.active:
            return False
        a, b = self.sel.range()
        ia, ib = col_to_index(self.value, a), col_to_index(self.value, b)
        self.value = self.value[:ia] + self.value[ib:]
        self.cursor = a
        self.sel.clear()
        if self.max_len and len(self.value) > self.max_len:
            self.value = self.value[: self.max_len]
        return True

    def _insert(self, text: str) -> None:
        if self._delete_range():
            pass
        if self.max_len is not None and len(self.value) + len(text) > self.max_len:
            text = text[: max(0, self.max_len - len(self.value))]
        if not text:
            return
        idx = col_to_index(self.value, self.cursor)
        self.value = self.value[:idx] + text + self.value[idx:]
        self.cursor = index_to_col(self.value, idx + len(text))
        self._ensure_visible()

    def _copy(self, cut: bool) -> None:
        if self.sel.active:
            text = self.sel.selected_text(self.value)
        else:
            text = self.value
        if text:
            set_text(text)
        if cut and self.sel.active:
            self._delete_range()
        logger.debug("剪贴板操作 %s %d 字符", "剪切" if cut else "复制", len(text))

    def _paste(self) -> None:
        text = get_text()
        if text:
            self._insert(text.replace("\r\n", " ").replace("\n", " "))
            if self.on_change:
                self.on_change(self.value)

    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        style = theme.input if not self.focused else theme.focus
        attr = theme.resolve(style)
        buf.fill(self.x, self.y, self.w, self.h, " ", attr)
        left = self.x + 1
        vw = self._visible_width()
        text = self.value
        seg = text[col_to_index(text, self.viewport):]
        visible = seg[: col_to_index(seg, vw) if line_width(seg) > vw else len(seg)]
        if self.sel.active:
            a, b = self.sel.range()
            sel_a, sel_b = max(a, self.viewport), min(b, self.viewport + vw)
            base = self.x + 1
            sel_attr = theme.resolve(theme.selected)
            for col in range(max(0, sel_a - self.viewport), max(0, sel_b - self.viewport)):
                ch = char_at(visible, col) or " "
                buf.put_char(base + col, self.y, ch, sel_attr)
            # 未选中部分
            for col in range(vw):
                if col < sel_a - self.viewport or col >= sel_b - self.viewport:
                    ch = char_at(visible, col) or " "
                    buf.put_char(base + col, self.y, ch, attr)
        else:
            x = left
            for ch in visible:
                buf.put_char(x, self.y, ch, attr)
                x += char_width(ch)
        if self.focused and self.cursor - self.viewport < vw:
            cur = self.x + 1 + self.cursor - self.viewport
            ch = char_at(self.value, self.cursor) or " "
            buf.put_char(cur, self.y, ch, theme.resolve(theme.focus))

    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        name = ev.name
        if name == "enter":
            if self.on_submit:
                self.on_submit(self, self.value)
            return True
        if name == "backspace":
            if not self._delete_range():
                result = backspace_at(self.value, self.cursor)
                if result:
                    self.value, self.cursor = result
            return True
        if name == "delete":
            if not self._delete_range():
                self._move(1, select=False)
            return True
        if name == "left":
            self._move(-1, ev.shift)
            return True
        if name == "right":
            self._move(1, ev.shift)
            return True
        if name == "home":
            self._move(-1, ev.shift, jump=True)
            return True
        if name == "end":
            self._move(1, ev.shift, jump=True)
            return True
        if name == "ctrl+a":
            self.sel.start_selection(0)
            self.sel.extend(line_width(self.value))
            return True
        if name == "ctrl+c":
            self._copy(cut=False)
            return True
        if name == "ctrl+x":
            self._copy(cut=True)
            return True
        if name == "ctrl+v":
            self._paste()
            return True
        if ev.ch and ev.ch.isprintable() and not ev.ctrl and not ev.alt:
            self._insert(ev.ch)
            if self.on_change:
                self.on_change(self.value)
            return True
        return False

    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        if ev.kind == "click":
            app.focus(self)
            self.cursor = max(0, min(line_width(self.value),
                                     self.viewport + (ev.x - self.x - 1)))
            self._ensure_visible()
            return True
        return False

    def cursor_pos(self) -> tuple[int, int]:
        """系统光标应放置的屏幕坐标（文本光标格）。"""
        return (self.x + 1 + self.cursor - self.viewport, self.y)


# ---------------------------------------------------------------- TextArea

class TextArea(Widget):
    """多行文本编辑区：方向键/翻页/Home/End 导航，自动滚动，选择与剪贴板。"""

    def __init__(self, text: str = "", *,
                 on_change: Callable[[str], None] | None = None, **kw) -> None:
        super().__init__(focusable=True, **kw)
        self.lines = text.split("\n")
        if not self.lines:
            self.lines = [""]
        self.on_change = on_change
        self.cx = 0
        self.cy = 0
        self.desired_col = 0
        self.vx = 0
        self.vy = 0
        self.sel = Selection()
        self.sel_anchor: tuple[int, int] | None = None  # (y, x)
        self.sel_y: int | None = None

    # ---- 文本 ----
    def text(self) -> str:
        return "\n".join(self.lines)

    def set_text(self, text: str) -> None:
        self.lines = text.split("\n") or [""]
        self.cx = self.cy = 0
        self.desired_col = 0
        self.sel.clear()
        self.sel_anchor = None

    def _changed(self) -> None:
        if self.on_change:
            self.on_change(self.text())

    def _cur_line(self) -> str:
        return self.lines[self.cy]

    def _move(self, dy: int, dx: int, select: bool) -> None:
        if select and self.sel_anchor is None:
            self.sel_anchor = (self.cy, self.cx)
        limit_y = len(self.lines) - 1
        ny = max(0, min(self.cy + dy, limit_y))
        if dx:
            self.desired_col = self.cx + dx
            if dy == 0:
                self.desired_col = max(0, self.desired_col)
        self.cy = ny
        self.cx = min(self.desired_col, line_width(self._cur_line()))
        if not select:
            self.sel_anchor = None
            self.sel.clear()
        self._ensure_visible()
        self._sync_sel()

    def _sync_sel(self) -> None:
        """把 (anchor, 光标) 同步进 Selection 用于绘制。"""
        if self.sel_anchor is None:
            return
        a_y, a_x = self.sel_anchor
        if a_y == self.cy:
            if a_x == self.cx:
                self.sel.clear()
            else:
                self.sel.anchor, self.sel.cursor = min(a_x, self.cx), max(a_x, self.cx)
        else:
            self.sel.anchor, self.sel.cursor = a_x, self.cx
            self.sel_y_pairs = True

    def _selection_text(self) -> str:
        if self.sel_anchor is None:
            return ""
        a_y, a_x = self.sel_anchor
        y1, y2 = min(a_y, self.cy), max(a_y, self.cy)
        x1 = a_x if a_y <= self.cy else self.cx
        x2 = self.cx if a_y <= self.cy else a_x
        if y1 == y2:
            a, b = sorted((x1, x2))
            ia, ib = col_to_index(self.lines[y1], a), col_to_index(self.lines[y1], b)
            return self.lines[y1][ia:ib]
        parts = []
        for y in range(y1, y2 + 1):
            line = self.lines[y]
            if y == y1:
                parts.append(line[col_to_index(line, x1):])
            elif y == y2:
                parts.append(line[: col_to_index(line, x2)])
            else:
                parts.append(line)
        return "\n".join(parts)

    def _delete_sel(self) -> bool:
        if self.sel_anchor is None or (self.sel_anchor == (self.cy, self.cx)):
            return False
        a_y, a_x = self.sel_anchor
        y1, x1 = (a_y, a_x) if a_y <= self.cy else (self.cy, self.cx)
        y2, x2 = (self.cy, self.cx) if a_y <= self.cy else (a_y, a_x)
        if y1 == y2:
            line = self.lines[y1]
            i1, i2 = col_to_index(line, min(x1, x2)), col_to_index(line, max(x1, x2))
            self.lines[y1] = line[:i1] + line[i2:]
            self.cy, self.cx = y1, min(x1, x2)
        else:
            self.lines[y1] = self.lines[y1][: col_to_index(self.lines[y1], x1)]
            self.lines[y2] = self.lines[y2][col_to_index(self.lines[y2], x2):]
            self.lines[y1:y2 + 1] = [self.lines[y1] + self.lines[y2]]
            self.cy, self.cx = y1, x1
        self.sel_anchor = None
        self.sel.clear()
        self.desired_col = self.cx
        return True

    def _insert(self, text: str) -> None:
        self._delete_sel()
        for ch in text:
            if ch == "\n":
                line = self.lines[self.cy]
                idx = col_to_index(line, self.cx)
                self.lines[self.cy:self.cy + 1] = [line[:idx], line[idx:]]
                self.cy += 1
                self.cx = 0
            else:
                line = self.lines[self.cy]
                idx = col_to_index(line, self.cx)
                self.lines[self.cy] = line[:idx] + ch + line[idx:]
                self.cx += char_width(ch)
        self.desired_col = self.cx
        self._ensure_visible()
        self._changed()

    def _copy(self, cut: bool) -> None:
        sel_first = self.sel_anchor is not None
        text = self._selection_text() if sel_first else "\n".join(self.lines)
        if text:
            set_text(text)
        if cut and sel_first:
            self._delete_sel()
            self._changed()

    def _paste(self) -> None:
        text = get_text()
        if text:
            self._insert(text.replace("\r\n", "\n").replace("\r", "\n"))

    def _ensure_visible(self) -> None:
        vh = max(1, self.h - 2)
        if self.cy < self.vy:
            self.vy = self.cy
        elif self.cy >= self.vy + vh:
            self.vy = self.cy - vh + 1
        vw = max(1, self.w - 3)
        if self.cx < self.vx:
            self.vx = self.cx
        elif self.cx >= self.vx + vw:
            self.vx = self.cx - vw + 1

    # ---- 事件 ----
    def handle_key(self, ev: KeyEvent, app: Application) -> bool:
        name = ev.name
        if name == "left":
            self._move(0, -1, ev.shift)
            return True
        if name == "right":
            self._move(0, 1, ev.shift)
            return True
        if name == "up":
            self._move(-1, 0, ev.shift)
            return True
        if name == "down":
            self._move(1, 0, ev.shift)
            return True
        if name == "home":
            if ev.ctrl:
                self.desired_col = 0
                self.cy = 0
                self.cx = 0
            else:
                self.desired_col = 0
                self.cx = 0
            self._ensure_visible()
            return True
        if name == "end":
            if ev.ctrl:
                self.cy = len(self.lines) - 1
            self.desired_col = line_width(self._cur_line())
            self.cx = self.desired_col
            self._ensure_visible()
            return True
        if name == "pgup":
            self._move(-(max(1, self.h - 2)), 0, ev.shift)
            return True
        if name == "pgdn":
            self._move(max(1, self.h - 2), 0, ev.shift)
            return True
        if name == "backspace":
            if not self._delete_sel():
                if self.cx > 0:
                    result = backspace_at(self._cur_line(), self.cx)
                    if result:
                        self.lines[self.cy], self.cx = result
                elif self.cy > 0:
                    prev = self.lines[self.cy - 1]
                    self.desired_col = self.cx = line_width(prev)
                    self.lines[self.cy - 1] = prev + self.lines[self.cy]
                    del self.lines[self.cy]
                    self.cy -= 1
            self._ensure_visible()
            self._changed()
            return True
        if name == "delete":
            if not self._delete_sel():
                idx = col_to_index(self._cur_line(), self.cx)
                if idx < len(self._cur_line()):
                    self.lines[self.cy] = self._cur_line()[:idx] + self._cur_line()[idx + 1:]
                elif self.cy < len(self.lines) - 1:
                    self.lines[self.cy] += self.lines[self.cy + 1]
                    del self.lines[self.cy + 1]
            self._changed()
            return True
        if name == "enter":
            self._insert("\n")
            return True
        if name == "ctrl+a":
            self.sel_anchor = (0, 0)
            self.cy, self.cx = len(self.lines) - 1, line_width(self.lines[-1])
            return True
        if name == "ctrl+c":
            self._copy(cut=False)
            return True
        if name == "ctrl+x":
            self._copy(cut=True)
            return True
        if name == "ctrl+v":
            self._paste()
            return True
        if ev.ch and ev.ch.isprintable() and not ev.ctrl and not ev.alt:
            self._insert(ev.ch)
            return True
        return False

    def handle_mouse(self, ev: MouseEvent, app: Application) -> bool:
        if ev.kind == "click":
            app.focus(self)
            rel_y = ev.y - self.y - 1
            rel_x = ev.x - self.x - 1
            self.cy = max(0, min(len(self.lines) - 1, self.vy + rel_y))
            self.cx = max(0, min(line_width(self._cur_line()), self.vx + rel_x))
            self.desired_col = self.cx
            self.sel_anchor = None
            return True
        if ev.kind == "wheel":
            self.vy = max(0, min(max(0, len(self.lines) - (self.h - 2)),
                                 self.vy - ev.delta * 3))
            return True
        return False

    def cursor_pos(self) -> tuple[int, int]:
        """系统光标应放置的屏幕坐标（文本光标格）。"""
        return (self.x + 1 + self.cx - self.vx, self.y + 1 + self.cy - self.vy)

    # ---- 绘制 ----
    def draw(self, buf: CharBuffer, theme: Theme) -> None:
        attr = theme.resolve(theme.input)
        border = theme.resolve(theme.border)
        vw = max(1, self.w - 3)
        vh = max(1, self.h - 2)
        # 边框
        buf.fill(self.x, self.y, self.w, self.h, " ", border)
        buf.put_char(self.x + self.w - 1, self.y, "│", border)
        buf.put_char(self.x + self.w - 1, self.y + self.h - 1, "│", border)
        for yy in range(self.y + 1, self.y + self.h - 1):
            buf.put_char(self.x, yy, "│", border)
            buf.put_char(self.x + self.w - 1, yy, "│", border)
        for xx in range(self.x + 1, self.x + self.w - 1):
            buf.put_char(xx, self.y, "─", border)
            buf.put_char(xx, self.y + self.h - 1, "─", border)
        buf.put_char(self.x, self.y, "┌", border)
        buf.put_char(self.x + self.w - 1, self.y, "┐", border)
        buf.put_char(self.x, self.y + self.h - 1, "└", border)
        buf.put_char(self.x + self.w - 1, self.y + self.h - 1, "┘", border)

        sel_active = self.sel_anchor is not None and self.sel_anchor != (self.cy, self.cx)
        for row in range(vh):
            ly = self.vy + row
            if ly >= len(self.lines):
                break
            line = self.lines[ly]
            seg = line[col_to_index(line, self.vx):]
            bufx = self.x + 1
            col = 0
            for ch in seg:
                if col >= vw:
                    break
                w = char_width(ch)
                if col + w > vw:
                    break
                selected = False
                if sel_active:
                    y1, y2 = min(self.sel_anchor[0], self.cy), max(self.sel_anchor[0], self.cy)
                    if y1 <= ly <= y2:
                        x1 = self.sel_anchor[1] if ly == y1 else self.vx
                        x2 = self.cx if ly == y2 else 10**9
                        x1 = min(x1, line_width(line))
                        selected = self.vx + col >= x1 and self.vx + col < x2
                buf.put_char(bufx + col, self.y + 1 + row, ch,
                             theme.resolve(theme.selected) if selected else attr)
                col += w
        # 光标
        if self.focused:
            rel_y = self.cy - self.vy
            rel_x = self.cx - self.vx
            if 0 <= rel_y < vh and 0 <= rel_x < vw:
                ch = char_at(self.lines[self.cy], self.cx) or " "
                buf.put_char(self.x + 1 + rel_x, self.y + 1 + rel_y, ch,
                             theme.resolve(theme.focus))
        # 垂直滚动条
        content_h = len(self.lines)
        if content_h > vh:
            thumb = max(1, round(vh * vh / content_h))
            off = round(self.vy * (vh - thumb) / (content_h - vh)) if content_h > vh else 0
            sb = self.x + self.w - 1
            for row in range(vh):
                on = off <= row < off + thumb
                buf.put_char(sb, self.y + 1 + row, "█" if on else "░",
                             theme.resolve(theme.scrollbar))