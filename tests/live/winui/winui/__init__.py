"""winui：基于 Win32 控制台 API（纯 ctypes）的全屏 TUI 框架。

分层（洋葱模型）：
  - 实体层（entities）：控件、布局、事件、主题
  - 用例层（app）：事件循环、焦点、模态、定时器
  - 接口适配层（adapters）：CharBuffer 双缓冲、剪贴板
  - 框架与驱动层（drivers）：kernel32/user32 Console API
"""

from winui.app import Application
from winui.entities.events import KeyEvent, MouseEvent, ResizeEvent
from winui.entities.layout import (
                                   Align,
                                   FillLayout,
                                   GridLayout,
                                   HorizontalLayout,
                                   VerticalLayout,
)
from winui.entities.listbox import ListBox
from winui.entities.menu import Menu, MenuBar, MenuItem
from winui.entities.messagebox import MessageBox
from winui.entities.textbox import TextArea, TextBox
from winui.entities.theme import DEFAULT_THEME, Color, Style, Theme
from winui.entities.widgets import (
                                   Button,
                                   CheckBox,
                                   Container,
                                   Label,
                                   ProgressBar,
                                   StatusBar,
                                   Widget,
)

__version__ = "0.1.0"

__all__ = [
                                   "DEFAULT_THEME",
                                   "Align",
                                   "Application",
                                   "Button",
                                   "CheckBox",
                                   "Color",
                                   "Container",
                                   "FillLayout",
                                   "GridLayout",
                                   "HorizontalLayout",
                                   "KeyEvent",
                                   "Label",
                                   "ListBox",
                                   "Menu",
                                   "MenuBar",
                                   "MenuItem",
                                   "MessageBox",
                                   "MouseEvent",
                                   "ProgressBar",
                                   "ResizeEvent",
                                   "StatusBar",
                                   "Style",
                                   "TextArea",
                                   "TextBox",
                                   "Theme",
                                   "VerticalLayout",
                                   "Widget",
]