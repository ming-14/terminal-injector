"""布局算法（实体层）。

支持三种布局策略：
  - horizontal / vertical：主轴方向堆叠，未指定固定尺寸的子级按权重均分剩余空间
  - grid：表格排布，行列可按权重划分，子级通过 grid_row/col/span 属性定位
子级未指定的轴方向默认填满交叉轴。
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from winui.entities.widgets import Widget


class Align(Enum):
    """交叉轴对齐方式。"""
    START = 0
    CENTER = 1
    END = 2
    FILL = 3


def _cross_offset(child: Widget, cross_idx: int, axis_size: int,
                  align: Align) -> int:
    """计算子级在交叉轴上的起始偏移（相对 padding 内缘）。"""
    size = child.width if cross_idx == 0 else child.height
    fixed = size if size is not None else axis_size
    if align is Align.START:
        return 0
    if align is Align.CENTER:
        return max(0, (axis_size - fixed) // 2)
    if align is Align.END:
        return max(0, axis_size - fixed)
    return 0  # FILL 已由布局器填满


class Layout:
    """布局基类：把子级 Widget 排布进父容器矩形。"""

    def arrange(self, x: int, y: int, w: int, h: int,
                children: Iterable[Widget]) -> None:
        raise NotImplementedError


class StackLayout(Layout):
    """主/交叉轴堆叠布局的公共逻辑。"""

    def __init__(self, *, spacing: int = 0, padding: int = 0,
                 align: Align = Align.FILL) -> None:
        self.spacing = spacing
        self.padding = padding
        self.align = align

    def _axis(self) -> tuple[int, int, int]:
        # (主轴索引, 交叉轴索引, 主轴是否水平)
        raise NotImplementedError

    def arrange(self, x, y, w, h, children) -> None:
        _main, cross, main_is_x = self._axis()
        origin = (x, y)
        avail_main = (w if main_is_x else h) - 2 * self.padding
        avail_cross = (h if main_is_x else w) - 2 * self.padding
        children = list(children)
        n = len(children)
        fixed_sum = sum(
            (c.width if main_is_x else c.height) or 0 for c in children)
        flex_count = sum(
            1 for c in children if (c.width if main_is_x else c.height) is None)
        usable = avail_main - self.spacing * max(n - 1, 0)
        flex_main = max(0, (usable - fixed_sum) // flex_count) if flex_count else 0

        pos = 0
        for child in children:
            size = child.width if main_is_x else child.height
            main_size = size if size is not None else flex_main
            main_size = max(0, min(main_size, avail_main - pos))
            cross_fixed = child.height if main_is_x else child.width
            if cross_fixed is None or self.align is Align.FILL:
                cross_size = avail_cross
                cross_off = 0
            else:
                cross_size = cross_fixed
                cross_off = _cross_offset(child, cross, avail_cross, self.align)
            if main_is_x:
                child.set_rect(origin[0] + self.padding + pos,
                               origin[1] + self.padding + cross_off,
                               main_size, cross_size)
            else:
                child.set_rect(origin[0] + self.padding + cross_off,
                               origin[1] + self.padding + pos,
                               cross_size, main_size)
            pos += main_size + self.spacing


class VerticalLayout(StackLayout):
    """垂直堆叠：高度未指定者按权重均分。"""

    def _axis(self):
        return 1, 0, False


class HorizontalLayout(StackLayout):
    """水平堆叠：宽度未指定者按权重均分。"""

    def _axis(self):
        return 0, 1, True


class FillLayout(Layout):
    """唯一子级填满整个可用区域（默认容器行为）。"""

    def arrange(self, x, y, w, h, children) -> None:
        for child in children:
            child.set_rect(x, y, w, h)


class GridLayout(Layout):
    """表格布局：rows/cols 为权重列表（如 [1, 2, 1]），子级用 grid_row/col/span 定位。"""

    def __init__(self, rows: list[int], cols: list[int],
                 *, gap: int = 1, padding: int = 0) -> None:
        self.rows = rows
        self.cols = cols
        self.gap = gap
        self.padding = padding

    def _partition(self, total: int, weights: list[int]) -> list[int]:
        gaps = self.gap * (len(weights) - 1)
        usable = max(0, total - gaps)
        total_w = sum(weights) or 1
        sizes = [usable * w // total_w for w in weights]
        # 余数补偿：从前往后补足
        remainder = usable - sum(sizes)
        for i in range(remainder):
            sizes[i % len(sizes)] += 1
        return sizes

    def arrange(self, x, y, w, h, children) -> None:
        cell_w = self._partition(w - 2 * self.padding, self.cols)
        cell_h = self._partition(h - 2 * self.padding, self.rows)
        # 计算每格起点
        xs, ys = [self.padding], [self.padding]
        for i in range(len(cell_w) - 1):
            xs.append(xs[-1] + cell_w[i] + self.gap)
        for i in range(len(cell_h) - 1):
            ys.append(ys[-1] + cell_h[i] + self.gap)

        for child in children:
            r, c = child.grid_row, child.grid_col
            rs = max(1, child.grid_rowspan)
            cs = max(1, child.grid_colspan)
            r = min(r, len(ys) - 1)
            c = min(c, len(xs) - 1)
            gx = xs[c]
            gy = ys[r]
            gw = sum(cell_w[c:c + cs]) + self.gap * (cs - 1)
            gh = sum(cell_h[r:r + rs]) + self.gap * (rs - 1)
            child.set_rect(x + gx, y + gy, gw, gh)