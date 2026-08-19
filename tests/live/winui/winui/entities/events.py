"""输入事件模型（实体层）。

把驱动层的原始事件转换为面向 TUI 的语义事件：
  - KeyEvent：附带友好名称（如 "enter"、"ctrl+c"、"上"键→"up"），控件只需按名称匹配
  - MouseEvent：坐标落在控件区域内的鼠标动作
  - ResizeEvent：窗口尺寸变化
"""

from __future__ import annotations

from dataclasses import dataclass

# 功能键名称表：VK 值 -> 名称
_VK_NAMES: dict[int, str] = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "esc",
    0x20: "space", 0x21: "pgup", 0x22: "pgdn", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down",
    0x2D: "insert", 0x2E: "delete",
}
for _i in range(0x70, 0x88):  # F1..F24
    _VK_NAMES[_i] = f"f{_i - 0x70 + 1}"


@dataclass(frozen=True, slots=True)
class KeyEvent:
    vk: int
    ch: str | None
    ctrl: bool = False
    shift: bool = False
    alt: bool = False

    @property
    def name(self) -> str:
        """友好事件名。

        规则：
          - 纯字符（大写归小写）："a"、"中"
          - Ctrl+字母（控制字符）："ctrl+c"
          - 功能键："enter"/"up"/"f1"（修饰键见 ctrl/alt/shift 属性）
          - 带修饰的字母：Ctrl+字母已被控制字符覆盖，Alt 组合为 "alt+f"
          - Ctrl+功能键："ctrl+up"
        """
        if self.ch and self.ch.isprintable() and not self.ctrl and not self.alt:
            # 空格等既是可打印字符又是功能键，功能键名优先
            base = _VK_NAMES.get(self.vk)
            if base:
                return base
            return self.ch.lower() if ord(self.ch) < 0x100 else self.ch
        if self.ctrl and self.ch and ord(self.ch) < 0x20:
            # 控制字符（0x01~0x1A）映射为 ctrl+字母
            return f"ctrl+{chr(ord(self.ch) + 0x60)}"
        base = _VK_NAMES.get(self.vk)
        if base:
            # 功能键不携带修饰前缀，修饰状态请检查 ctrl/shift/alt 属性
            return base
        if 0x41 <= self.vk <= 0x5A:  # 带修饰的字母键
            mods = [m for m, flag in
                    (("ctrl", self.ctrl), ("alt", self.alt), ("shift", self.shift)) if flag]
            letter = chr(self.vk | 0x20)
            return "+".join([*mods, letter]) if mods else letter
        return f"vk{self.vk}"

    def __str__(self) -> str:
        return f"<Key {self.name}>"


@dataclass(frozen=True, slots=True)
class MouseEvent:
    x: int
    y: int
    kind: str            # "click" / "dblclick" / "release" / "move" / "wheel"
    button: str | None   # click/release 时的按键；wheel 时为 "wheel"
    delta: int = 0       # wheel 方向（+1 上 / -1 下）

    @property
    def inside(self) -> bool:  # 占位语义，实际命中检测由容器完成
        return True


@dataclass(frozen=True, slots=True)
class ResizeEvent:
    width: int
    height: int


def from_raw(raw) -> KeyEvent | MouseEvent | ResizeEvent | None:
    """把驱动原始事件翻译为 TUI 事件；无法翻译的返回 None。"""
    t = type(raw).__name__
    if t == "RawKeyEvent":
        if raw.down:
            return KeyEvent(vk=raw.vk, ch=raw.ch, ctrl=raw.ctrl,
                            shift=raw.shift, alt=raw.alt)
        return None
    if t == "RawMouseEvent":
        if raw.wheel:
            return MouseEvent(raw.x, raw.y, "wheel", "wheel", raw.wheel)
        if raw.double_click:
            return MouseEvent(raw.x, raw.y, "dblclick", raw.button)
        if raw.button and not raw.moved:
            return MouseEvent(raw.x, raw.y, "click", raw.button)
        if raw.moved and raw.button is None:
            return MouseEvent(raw.x, raw.y, "move", None)
        return MouseEvent(raw.x, raw.y, "release", raw.button)
    if t == "RawResizeEvent":
        return ResizeEvent(raw.width, raw.height)
    return None