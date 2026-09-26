# tests — tigui 的标准库测试(仅用 unittest,无第三方依赖)
#
# 运行(工作目录须为项目根目录 gui/):
#   python -m unittest discover -s tests -t . -v
#   python tests/test_discover.py          # 单文件也可直接跑
#   python tests/test_wiring.py
#
# 测试全部使用临时目录,不依赖真实 terminal_injector.exe / injected.dll,
# 也不会真的注入任何进程。

import sys
from pathlib import Path

# 保证任意启动方式(含 `python tests/xxx.py`)都能 import 到 tigui 包
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
