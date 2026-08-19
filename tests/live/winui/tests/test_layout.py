"""布局算法单元测试。"""

from winui.entities.layout import (
    Align,
    FillLayout,
    GridLayout,
    HorizontalLayout,
    VerticalLayout,
)
from winui.entities.widgets import Label


def make(text: str, **kw) -> Label:
    return Label(text, **kw)


class TestVertical:
    def test_fixed_and_flex(self):
        layout = VerticalLayout(spacing=0, padding=0)
        a = make("a", height=2)
        b = make("b")  # 自适应
        c = make("c", height=1)
        layout.arrange(0, 0, 10, 8, [a, b, c])
        assert (a.x, a.y, a.w, a.h) == (0, 0, 10, 2)
        assert (b.y, b.h) == (2, 5)
        assert (c.y, c.h) == (7, 1)

    def test_spacing(self):
        layout = VerticalLayout(spacing=1, padding=0)
        a = make("a", height=1)
        b = make("b", height=1)
        layout.arrange(0, 0, 10, 10, [a, b])
        assert a.y == 0
        assert b.y == 2

    def test_padding(self):
        layout = VerticalLayout(padding=1, spacing=0)
        a = make("a")
        layout.arrange(0, 0, 10, 10, [a])
        assert a.x == 1
        assert a.y == 1
        assert a.w == 8
        assert a.h == 8

    def test_cross_align_start(self):
        layout = VerticalLayout(padding=0, spacing=0, align=Align.START)
        a = make("a", width=4, height=2)
        layout.arrange(0, 0, 10, 2, [a])
        assert (a.x, a.w) == (0, 4)

    def test_cross_align_center(self):
        layout = VerticalLayout(padding=0, spacing=0, align=Align.CENTER)
        a = make("a", width=4, height=2)
        layout.arrange(0, 0, 10, 2, [a])
        assert (a.x, a.w) == (3, 4)


class TestHorizontal:
    def test_splits(self):
        layout = HorizontalLayout(spacing=0, padding=0)
        a = make("a")
        b = make("b", width=3)
        layout.arrange(0, 0, 10, 4, [a, b])
        assert a.w == 7
        assert b.w == 3
        assert b.x == 7


class TestFill:
    def test_fills(self):
        layout = FillLayout()
        a = make("a")
        layout.arrange(2, 3, 10, 5, [a])
        assert (a.x, a.y, a.w, a.h) == (2, 3, 10, 5)


class TestGrid:
    def test_partition(self):
        layout = GridLayout(rows=[1, 1], cols=[1, 2], gap=0, padding=0)
        a, b, c, d = make("a"), make("b"), make("c"), make("d")
        a.grid_row, a.grid_col = 0, 0
        b.grid_row, b.grid_col = 0, 1
        c.grid_row, c.grid_col = 1, 0
        d.grid_row, d.grid_col = 1, 1
        layout.arrange(0, 0, 9, 2, [a, b, c, d])
        assert a.w == 3
        assert b.w == 6
        assert b.x == 3

    def test_span(self):
        layout = GridLayout(rows=[1, 1], cols=[1, 1], gap=0, padding=0)
        a = make("a")
        a.grid_row, a.grid_col, a.grid_colspan = 0, 0, 2
        b = make("b")
        b.grid_row, b.grid_col = 1, 0
        layout.arrange(0, 0, 4, 2, [a, b])
        assert a.w == 4
        assert b.w == 2