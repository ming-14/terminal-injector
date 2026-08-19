# winui

基于 Win32 控制台 API（纯 ctypes，零第三方依赖）的全屏 TUI 框架。

## 特性

- **全屏渲染**：实时适配窗口尺寸（含 ConPTY 伪终端），差量输出（仅重绘变化区域）
- **东亚宽字符**：中文/日文等宽字符正确处理（LEADING/TRAILING 单元格标记）
- **完整控件集**：Label、Button、CheckBox、ListBox（单/多选+滚动条）、TextBox（单行）、
  TextArea（多行编辑）、ProgressBar、StatusBar、MessageBox（模态）、MenuBar（下拉菜单）
- **文本编辑能力**：光标移动（宽字符感知）、选择、系统剪贴板复制/剪切/粘贴（Ctrl+C/X/V）
- **交互**：Tab 焦点环、鼠标点击/滚轮/双击、窗口 resize 自适应、定时器动画
- **主题**：16 色调色板 + Style 修饰（bold/underline/reverse），可自定义
- **布局**：垂直/水平堆叠（weight 分配）、Grid 表格、Fill

## 架构（洋葱模型）

```
winui/
├── entities/    实体层：控件、布局、事件、主题（纯领域逻辑）
├── app.py       用例层：事件循环、焦点管理、模态栈、定时器
├── adapters/    接口适配层：CharBuffer 双缓冲+差量、剪贴板
└── drivers/     框架与驱动层：kernel32/user32 Console API（ctypes）
```

依赖规则：`entities ← app ← adapters ← drivers`，内层不感知外层实现。

## 快速开始

```python
from winui import Application, Button, Container, Label, TextBox, VerticalLayout

app = Application(title="hello")
app.set_root(Container(layout=VerticalLayout(spacing=1, padding=1)))
name = TextBox("world")
app.add(Label("你叫什么？"))
app.add(name)
app.add(Button("提交", on_click=lambda b: app.quit()))
app.run()
```

开发/演示：`python examples/demo.py`（完整示例，Ctrl+Q 退出）。

## 测试

```bash
pip install pytest ruff
python -m pytest tests          # 单元 + e2e（需桌面 pywezterm 目录，或 PYWEZTERM_PATH）
python -m ruff check .
```

PTY e2e 使用真实 ConPTY 运行 demo 并断言渲染结果、键盘交互、resize 重排与退出码。