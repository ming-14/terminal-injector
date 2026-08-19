"""CharBuffer 单元测试：宽字符、覆盖、差量输出。"""


from winui.adapters.buffer import CharBuffer, char_width
from winui.entities.theme import COMMON_LVB_LEADING_BYTE, COMMON_LVB_TRAILING_BYTE


class TestCharWidth:
    def test_ascii(self):
        assert char_width("a") == 1

    def test_cjk(self):
        assert char_width("中") == 2
        assert char_width("文") == 2

    def test_neutral_block_chars(self):
        assert char_width("█") == 1
        assert char_width("░") == 1


class TestPutText:
    def test_plain(self):
        buf = CharBuffer(10, 3)
        buf.put_text(0, 1, "hello")
        assert buf.get(0, 1).ch == "h"
        assert buf.get(4, 1).ch == "o"

    def test_cjk_spans_two_cells(self):
        buf = CharBuffer(6, 1)
        buf.put_text(0, 0, "中文")
        assert buf.get(0, 0).ch == "中"
        assert buf.get(0, 0).wide is True
        assert buf.get(1, 0).ch is None  # 尾格
        assert buf.get(2, 0).ch == "文"
        assert buf.get(3, 0).ch is None

    def test_wide_char_clipped_at_row_end(self):
        buf = CharBuffer(3, 1)
        buf.put_text(2, 0, "中文")
        assert buf.get(2, 0).ch == "中" or buf.get(2, 0).ch == " "
        assert buf.get(2, 0).wide is False  # 放不下则降级

    def test_ascii_after_cjk(self):
        buf = CharBuffer(6, 1)
        buf.put_text(0, 0, "中x")
        assert buf.get(1, 0).ch is None  # 尾格仍在
        assert buf.get(2, 0).ch == "x"
        buf.put_text(0, 0, "a")
        assert buf.get(0, 0).ch == "a"
        assert buf.get(1, 0).ch == " "  # 宽字符配对被清理

    def test_mixed(self):
        buf = CharBuffer(8, 1)
        buf.put_text(0, 0, "a中文b")
        assert [buf.get(i, 0).ch for i in range(7)] == ["a", "中", None, "文", None, "b", " "]


class TestExtract:
    def test_wide_flags(self):
        buf = CharBuffer(6, 1)
        buf.put_text(0, 0, "中a")
        matrix = buf._extract(0, 0, 6, 1)
        row = matrix[0]
        assert row[0] == ("中", COMMON_LVB_LEADING_BYTE)
        assert row[1] == (" ", COMMON_LVB_TRAILING_BYTE)
        assert row[2] == ("a", 0)


class TestDiff:
    def _clear_init(self, buf: CharBuffer):
        buf.render_diff()  # 建立首帧快照

    def test_no_change(self):
        buf = CharBuffer(5, 2)
        buf.put_text(0, 0, "ab")
        self._clear_init(buf)
        assert buf.render_diff() == []

    def test_single_cell_change(self):
        buf = CharBuffer(5, 2)
        self._clear_init(buf)
        buf.put_char(2, 1, "X")
        regions = buf.render_diff()
        assert len(regions) == 1
        x, y, w, h, mat = regions[0]
        assert (x, y, w, h) == (2, 1, 1, 1)
        assert mat[0][0][0] == "X"

    def test_wide_change_includes_tail(self):
        buf = CharBuffer(6, 1)
        self._clear_init(buf)
        buf.put_text(0, 0, "中")
        regions = buf.render_diff()
        assert len(regions) == 1
        _, _, _w, _, mat = regions[0]
        assert mat[0][0][0] == "中"
        assert mat[0][0][1] & COMMON_LVB_LEADING_BYTE
        assert mat[0][1][1] & COMMON_LVB_TRAILING_BYTE

    def test_overwrite_wide_with_ascii_covers_tail(self):
        buf = CharBuffer(4, 1)
        buf.put_text(0, 0, "中")
        self._clear_init(buf)
        buf.put_char(0, 0, "x")
        regions = buf.render_diff()
        total_w = sum(w for _, _, w, _, _ in regions)
        assert total_w >= 2  # 首格+尾格都在差量里

    def test_multiple_changes_merged_per_row(self):
        buf = CharBuffer(10, 1)
        self._clear_init(buf)
        buf.put_char(1, 0, "a")
        buf.put_char(2, 0, "b")
        regions = buf.render_diff()
        assert len(regions) == 1
        _, _, w, _, _ = regions[0]
        assert w == 2