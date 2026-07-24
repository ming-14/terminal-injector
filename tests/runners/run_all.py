"""Phase 6 测试主入口。

运行所有自动化测试套件：
  - test_cmd_basic: cmd 键盘基础（字符输入/退格/方向键/Home End）
  - test_unicode: 中文/emoji 输入（UTF-8 字节验证）
  - test_python_curses_mouse: python 鼠标 TUI 自检
  - test_vim_mouse: vim 鼠标点击

用法：
  python tests/runners/run_all.py              # 运行全部
  python tests/runners/run_all.py cmd unicode  # 运行指定套件
  python tests/runners/run_all.py --list       # 列出可用套件

注意：每个套件独立启动 cmd + WT(mediator) + 注入，测试后清理。
      测试期间请勿手动操作 WT 窗口，避免干扰 SendInput。
"""
import os
import sys
import time
import importlib
import traceback

sys.path.insert(0, os.path.dirname(__file__))

# 测试套件注册表：(名称, 模块名, 描述)
SUITES = [
    ("cmd", "test_cmd_basic", "cmd 键盘基础（字符输入/退格/方向键/Home End）"),
    ("unicode", "test_unicode", "中文/emoji 输入（UTF-8 字节验证）"),
    ("mouse", "test_python_curses_mouse", "python 鼠标 TUI 自检（ReadConsoleInputW）"),
    ("vim", "test_vim_mouse", "vim 鼠标点击（SGR 1006 验证）"),
    ("signal", "test_signal", "Ctrl+C 信号中断 python 死循环（Phase 7）"),
    ("phase8", "test_phase8", "Phase 8 高级特性（Title OSC 序列验证）"),
    ("phase9", "test_phase9", "Phase 9 自保护（Alloc/Attach/Free/CloseHandle 静默拦截）"),
]


def list_suites() -> None:
    print("可用测试套件：")
    for name, _, desc in SUITES:
        print("  {:12s} {}".format(name, desc))


def run_suite(name: str, module_name: str) -> int:
    """运行单个测试套件，返回失败数。"""
    print("\n" + "=" * 60)
    print("运行套件: {} ({})".format(name, module_name))
    print("=" * 60)
    try:
        mod = importlib.import_module(module_name)
        return mod.run()
    except Exception as e:
        print("[ERROR] 套件 {} 异常: {}".format(name, e))
        traceback.print_exc()
        return 1


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] in ("--list", "-l"):
        list_suites()
        return 0

    if args and args[0] in ("--help", "-h"):
        print(__doc__)
        return 0

    # 确定要运行的套件
    if args:
        # 按名称筛选
        selected = []
        for arg in args:
            match = [s for s in SUITES if s[0] == arg]
            if not match:
                print("[ERROR] 未知套件: {}".format(arg))
                list_suites()
                return 2
            selected.extend(match)
    else:
        selected = SUITES

    print("Phase 6 输入链路自动化测试")
    print("将运行 {} 个套件: {}".format(len(selected), ", ".join(s[0] for s in selected)))
    print("注意：测试期间请勿手动操作 WT 窗口")
    time.sleep(1)

    results = {}
    total_failures = 0
    for name, module_name, _ in selected:
        failures = run_suite(name, module_name)
        results[name] = failures
        total_failures += failures
        # 套件间间隔（确保进程清理完成）
        time.sleep(2.0)

    print("\n" + "=" * 60)
    print("汇总")
    print("=" * 60)
    for name, _, _ in selected:
        f = results.get(name, 0)
        status = "通过" if f == 0 else "失败({})".format(f)
        print("  {:12s} {}".format(name, status))
    print("-" * 60)
    if total_failures == 0:
        print("全部通过")
    else:
        print("共 {} 项失败".format(total_failures))
    return total_failures


if __name__ == "__main__":
    sys.exit(main())
