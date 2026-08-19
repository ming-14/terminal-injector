"""控件行为单元测试：文本框编辑/选择、文本区、列表、复选框、消息框。"""

from winui.entities.events import KeyEvent, MouseEvent
from winui.entities.listbox import ListBox
from winui.entities.messagebox import MessageBox
from winui.entities.textbox import TextArea, TextBox, col_to_index, index_to_col
from winui.entities.widgets import Container


class StubApp:
    """测试用最小 Application 替身（焦点沿父链标记，与真实焦点语义一致）。"""
    def __init__(self):
        self.focused = None
        self.modals = []
        self.popped = 0

    def _clear(self, container):
        for c in container.children:
            c.focused = False
            if isinstance(c, Container):
                self._clear(c)

    def focus(self, widget):
        if self.focused is not None:
            self.focused.focused = False
        if self.focused is not None and self.focused.parent is not None:
            root = self.focused
            while root.parent is not None:
                root = root.parent
            self._clear(root)
        self.focused = widget
        if widget is not None:
            node = widget
            while node is not None:
                node.focused = True
                node = node.parent

    def focus_next(self, backward=False):
        pass

    def push_modal(self, widget):
        self.modals.append(widget)

    def pop_modal(self):
        self.popped += 1
        if self.modals:
            self.modals.pop()


def key(name: str = "", ch: str | None = None, *, ctrl=False, shift=False, alt=False) -> KeyEvent:
    vk_map = {"enter": 0x0D, "backspace": 0x08, "delete": 0x2E, "left": 0x25,
              "right": 0x27, "home": 0x24, "end": 0x23, "up": 0x26, "down": 0x28,
              "pgup": 0x21, "pgdn": 0x22, "space": 0x20, "tab": 0x09, "esc": 0x1B}
    vk = vk_map.get(name, 0)
    if ch is None and name:
        ch = {"enter": "\r", "tab": "\t", "space": " "}.get(name)
    return KeyEvent(vk=vk, ch=ch, ctrl=ctrl, shift=shift, alt=alt)


def chars(ev: KeyEvent, text: str):
    for ch in text:
        yield KeyEvent(vk=ord(ch), ch=ch)


class TestColIndex:
    def test_cjk_columns(self):
        assert col_to_index("中文", 0) == 0
        assert col_to_index("中文", 2) == 1  # 宽字符尾格位置
        assert col_to_index("中文", 4) == 2
        assert index_to_col("中文", 1) == 2

    def test_snap(self):
        from winui.entities.textbox import snap_left
        assert snap_left("中文", 3) == 2


class TestTextBox:
    def make(self):
        return TextBox()

    def test_insert(self):
        tb = self.make()
        tb._insert("ab")
        assert tb.value == "ab"
        assert tb.cursor == 2

    def test_insert_cjk(self):
        tb = self.make()
        tb._insert("中文")
        assert tb.value == "中文"
        assert tb.cursor == 4

    def test_insert_middle(self):
        tb = self.make()
        tb._insert("abcd")
        tb.cursor = 2
        tb._insert("X")
        assert tb.value == "abXcd"
        assert tb.cursor == 3

    def test_backspace(self):
        tb = self.make()
        tb._insert("中文a")
        assert tb.value == "中文a"
        tb.handle_key(key("backspace"), StubApp())
        assert tb.value == "中文"
        tb.handle_key(key("backspace"), StubApp())
        assert tb.value == "中"

    def test_move_and_end(self):
        tb = self.make()
        tb._insert("ab")
        tb.handle_key(key("left"), StubApp())
        assert tb.cursor == 1
        tb.handle_key(key("end"), StubApp())
        assert tb.cursor == 2

    def test_selection_and_copy(self):
        tb = self.make()
        tb._insert("abcdef")
        tb.handle_key(key("home"), StubApp())
        # shift+right x3
        for _ in range(3):
            tb.handle_key(key("right", shift=True), StubApp())
        assert tb.sel.range() == (0, 3)
        assert tb.sel.selected_text(tb.value) == "abc"

    def test_delete_range(self):
        tb = self.make()
        tb._insert("abcdef")
        tb.handle_key(key("home"), StubApp())
        for _ in range(2):
            tb.handle_key(key("right", shift=True), StubApp())
        assert tb._delete_range()
        assert tb.value == "cdef"

    def test_enter_submits(self):
        app = StubApp()
        result = []
        tb = TextBox(on_submit=lambda box, v: result.append(v))
        tb.handle_key(key("enter"), app)
        assert result == [""]


class TestTextArea:
    def make(self):
        return TextArea("a\nb")

    def test_initial(self):
        ta = self.make()
        assert ta.lines == ["a", "b"]

    def test_insert_text(self):
        ta = self.make()
        ta.set_text("")
        for ev in chars(None, "你好"):
            ta._insert(ev.ch)
        assert ta.text() == "你好"

    def test_insert_newline(self):
        ta = self.make()
        ta.set_text("ab")
        ta.cx = 1
        ta._insert("\n")
        assert ta.lines == ["a", "b"]

    def test_backspace_joins_lines(self):
        ta = self.make()
        ta.cy = 1
        ta.cx = 0
        assert ta.handle_key(key("backspace"), StubApp())
        assert ta.lines == ["ab"]

    def test_delete_joins_lines(self):
        ta = self.make()
        ta.cy = 0
        ta.cx = 1
        assert ta.handle_key(key("delete"), StubApp())
        assert ta.lines == ["a", "b"] or ta.lines == ["ab"]

    def test_arrow_moves(self):
        ta = self.make()
        ta.cy = 1
        ta.cx = 1
        ta.desired_col = 1
        ta.handle_key(key("up"), StubApp())
        assert ta.cy == 0
        assert ta.cx == 1  # desired_col 保持

    def test_arrow_up_shorter_line_clamps(self):
        ta = TextArea("短\n很长很长的第二行")
        ta.cy = 1
        ta.cx = 6
        ta.desired_col = 6
        ta.handle_key(key("up"), StubApp())
        assert ta.cy == 0
        assert ta.cx == 2  # “短”是宽字符，占 2 列

    def test_home_end(self):
        ta = TextArea("abcdef")
        ta.cx = 3
        ta.handle_key(key("home"), StubApp())
        assert ta.cx == 0
        ta.handle_key(key("end"), StubApp())
        assert ta.cx == 6

    def test_ctrl_home_end(self):
        ta = TextArea("a\nb\nc")
        ta.cy = 1
        ta.handle_key(key("home", ctrl=True), StubApp())
        assert (ta.cy, ta.cx) == (0, 0)
        ta.handle_key(key("end", ctrl=True), StubApp())
        assert (ta.cy, ta.cx) == (2, 1)

    def test_selection_text(self):
        ta = TextArea("abc\ndef\nghi")
        ta.sel_anchor = (0, 1)
        ta.cy, ta.cx = 2, 1
        assert ta._selection_text() == "bc\ndef\ng"

    def test_delete_selection_cross_lines(self):
        ta = TextArea("abc\ndef")
        ta.sel_anchor = (0, 1)
        ta.cy, ta.cx = 1, 2
        assert ta._delete_sel()
        assert ta.text() == "af"
        assert (ta.cy, ta.cx) == (0, 1)

    def test_backspace_cjk(self):
        ta = TextArea("中文")
        ta.cx = 2  # “文”首格前
        ta.handle_key(key("backspace"), StubApp())
        assert ta.text() == "文"
        assert ta.cx == 0

    def test_backspace_cjk_tail(self):
        ta = TextArea("中a")
        ta.cx = 3  # a 之后
        ta.handle_key(key("backspace"), StubApp())
        assert ta.text() == "中"
        assert ta.cx == 2

    def test_pgdn_pgup(self):
        ta = TextArea("\n".join(str(i) for i in range(20)))
        ta.handle_key(key("pgdn"), StubApp())
        assert ta.cy > 0


class TestListBox:
    def make(self):
        return ListBox(items=[f"item{i}" for i in range(10)])

    def test_step(self):
        lb = self.make()
        app = StubApp()
        lb.handle_key(key("down"), app)
        assert lb.selected == 0  # 初始 -1，第一步到 0
        lb.handle_key(key("down"), app)
        assert lb.selected == 1
        lb.handle_key(key("up"), app)
        assert lb.selected == 0

    def test_home_end(self):
        lb = self.make()
        app = StubApp()
        lb.handle_key(key("end"), app)
        assert lb.selected == 9
        lb.handle_key(key("home"), app)
        assert lb.selected == 0

    def test_viewport_follows(self):
        lb = ListBox(items=[str(i) for i in range(100)], height=10)
        lb.set_rect(0, 0, 20, 10)
        app = StubApp()
        for _ in range(50):
            lb.handle_key(key("down"), app)
        assert lb.selected == 49
        assert lb.viewport <= 49 < lb.viewport + lb.h

    def test_select_callback(self):
        results = []
        lb = ListBox(items=[f"item{i}" for i in range(10)],
                     on_select=lambda box, i, name: results.append((i, name)))
        lb.handle_key(key("down"), StubApp())
        lb.handle_key(key("enter"), StubApp())
        assert results == [(0, "item0")]

    def test_multi_toggle(self):
        lb = ListBox(multi=True)
        app = StubApp()
        lb.selected = 0
        lb.handle_key(key("space"), app)
        assert 0 in lb.multi_sel
        lb.handle_key(key("space"), app)
        assert 0 not in lb.multi_sel

    def test_mouse_click(self):
        lb = self.make()
        lb.set_rect(0, 0, 20, 5)
        ev = MouseEvent(1, 3, "click", "left")
        lb.handle_mouse(ev, StubApp())
        assert lb.selected == 3

    def test_mouse_wheel(self):
        lb = ListBox(items=[str(i) for i in range(100)], height=5)
        lb.handle_mouse(MouseEvent(1, 1, "wheel", "wheel", delta=-1), StubApp())
        assert lb.viewport == 3
        lb.handle_mouse(MouseEvent(1, 1, "wheel", "wheel", delta=1), StubApp())
        assert lb.viewport == 0


class TestMessageBox:
    def make(self, on_close=None):
        box = MessageBox("标题", "第1行\n第2行", ["确定", "取消"], on_close)
        box.arrange(0, 0, box.width, box.height)
        return box

    def test_enter_activates_focused_button(self):
        app = StubApp()
        result = []
        box = self.make(on_close=lambda i: result.append(i))
        box._attach(app)
        app.focus(box._buttons[0])
        assert box.handle_key(key("enter"), app) is True
        assert result == [0]
        assert app.popped == 1  # 模态已移除

    def test_space_activates_button(self):
        app = StubApp()
        result = []
        box = self.make(on_close=lambda i: result.append(i))
        box._attach(app)
        app.focus(box._buttons[1])
        assert box.handle_key(key("space"), app) is True
        assert result == [1]

    def test_esc_closes_with_minus_one(self):
        app = StubApp()
        result = []
        box = self.make(on_close=lambda i: result.append(i))
        box._attach(app)
        assert box.handle_key(key("esc"), app) is True
        assert result == [-1]
        assert app.popped == 1

    def test_arrow_switches_buttons(self):
        app = StubApp()
        box = self.make()
        app.focus(box._buttons[0])
        assert box.handle_key(key("right"), app) is True
        assert app.focused is box._buttons[1]
        assert box.handle_key(key("left"), app) is True
        assert app.focused is box._buttons[0]

    def test_mouse_click_button(self):
        app = StubApp()
        result = []
        box = self.make(on_close=lambda i: result.append(i))
        box._attach(app)
        btn = box._buttons[1]
        box.handle_mouse(MouseEvent(btn.x + 1, btn.y, "click", "left"), app)
        assert result == [1]
        assert app.popped == 1