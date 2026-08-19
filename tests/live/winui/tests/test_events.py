"""输入事件翻译与命名测试。"""

from winui.drivers.console import RawKeyEvent, RawMouseEvent, RawResizeEvent
from winui.entities.events import KeyEvent, MouseEvent, ResizeEvent, from_raw


class TestKeyName:
    def test_plain_letter(self):
        assert KeyEvent(vk=0x41, ch="a").name == "a"

    def test_shift_letter(self):
        assert KeyEvent(vk=0x41, ch="A", shift=True).name == "a"

    def test_enter(self):
        assert KeyEvent(vk=0x0D, ch="\r").name == "enter"

    def test_arrows(self):
        assert KeyEvent(vk=0x26, ch=None).name == "up"

    def test_ctrl_c(self):
        assert KeyEvent(vk=0x43, ch="\x03", ctrl=True).name == "ctrl+c"

    def test_ctrl_arrow(self):
        # 功能键不携带修饰前缀，ctrl 状态用属性判断
        ev = KeyEvent(vk=0x26, ch=None, ctrl=True)
        assert ev.name == "up"
        assert ev.ctrl is True

    def test_alt_f(self):
        assert KeyEvent(vk=0x46, ch=None, alt=True).name == "alt+f"

    def test_cjk_char(self):
        assert KeyEvent(vk=0, ch="中").name == "中"

    def test_f_key(self):
        assert KeyEvent(vk=0x70, ch=None).name == "f1"

    def test_tab_shift(self):
        # 功能键不携带修饰前缀，修饰状态用属性判断
        ev = KeyEvent(vk=0x09, ch="\t", shift=True)
        assert ev.name == "tab"
        assert ev.shift is True


class TestFromRaw:
    def test_key_down(self):
        raw = RawKeyEvent(down=True, vk=0x0D, ch="\r", ctrl=False, shift=False, alt=False)
        ev = from_raw(raw)
        assert isinstance(ev, KeyEvent)
        assert ev.name == "enter"

    def test_key_up_ignored(self):
        raw = RawKeyEvent(down=False, vk=0x41, ch="a", ctrl=False, shift=False, alt=False)
        assert from_raw(raw) is None

    def test_mouse_click(self):
        raw = RawMouseEvent(3, 4, moved=False, double_click=False, wheel=0, button="left")
        ev = from_raw(raw)
        assert isinstance(ev, MouseEvent)
        assert ev.kind == "click"
        assert (ev.x, ev.y) == (3, 4)

    def test_mouse_wheel(self):
        raw = RawMouseEvent(1, 1, moved=False, double_click=False, wheel=1, button=None)
        ev = from_raw(raw)
        assert ev.kind == "wheel"
        assert ev.delta == 1

    def test_resize(self):
        raw = RawResizeEvent(120, 30)
        ev = from_raw(raw)
        assert isinstance(ev, ResizeEvent)
        assert (ev.width, ev.height) == (120, 30)