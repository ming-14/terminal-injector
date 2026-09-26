# gui.py — terminal-injector 图形化管理界面(入口)
#
# 实现位于同目录 tigui/ 包:
#   i18n.py     界面文本(按系统 UI 语言中英自动切换)
#   paths.py    exe/dll 定位(terminal_injector.exe 与 injected.dll 须与本文件同目录)
#   discover.py 缺失时在后台线程按深度/超时限制递归搜索 exe/dll(常量可配)
#   winapi.py   ctypes:查 injected.dll 基址 / 解码子进程输出
#   backend.py  terminal_injector.exe 调用层(无 tkinter 依赖,可单独测试)
#   tasks.py    后台任务(线程 + 队列 + busy)
#   ui/         界面构件(Mixin):menu / toolbar / table / logview / statusbar / theme / spy
#   app.py      主窗口装配与动作编排
#
# 测试(标准库 unittest,不依赖真实二进制、不注入任何进程):
#   python -m unittest discover -s tests -t . -v
#
# 功能:
#   - 列出进程(调用 --list-targets --json --all 取全量,再按『仅显示可注入』过滤),
#     支持过滤与自动刷新
#   - 一键注入选中进程(--inject,可选管道名),远程卸载(--unload-remote)
#   - 卸载所需的 injected.dll 基址用 ctypes 查询目标进程模块,无需手工输入
#   - 缺失 exe/dll 时自动在后台递归探测(深度/超时/排除目录见 tigui/discover.py)
#   - 窗口探测准星(Spy++ 风格):拖到任意窗口,自动标记其全部关联进程
#   - 行着色:进程名色(cmd 黑 / pwsh 蓝 / bash 橙 / python 黄 / 其余蓝,
#     不可注入为灰字) + 绿底状态(已注入标准绿,GUI 启动后新起浅绿)
#   - 实时日志面板(时间戳+着色)、状态栏(版本/路径/选中进程)
# 依赖:仅 Python 标准库(tkinter / ctypes / subprocess / json)
# i18n:界面文本按系统 UI 语言自动切换(中文/英文),不提供手动切换

import os
import sys

# 双击 .py / 快捷方式启动时脚本目录已在 sys.path[0];此处兜底,
# 保证以其他方式加载(如被 import)时仍能导入 tigui 包。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tigui.app import main  # noqa: E402 - 需先完成 sys.path 兜底

if __name__ == "__main__":
    main()
