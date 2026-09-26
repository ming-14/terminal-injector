# test_table_color.py — 行着色优先级 / 工具自身识别测试
#
# 覆盖两件不能靠肉眼回归的事:
#   1. is_tool_process 的判定边界(GUI 自身按 PID、注入器按进程名,
#      不得误伤同名的其它进程)
#   2. 标签配置顺序即优先级(Tkinter 中先配置者胜),紫底必须压过两种绿底
# 用 Fake Tree 记录 tag_configure / insert,故:不创建 Tk 窗口、不开主循环、
# 不依赖真实 terminal_injector.exe,也不会真的注入任何进程。
#
# 运行:python -m unittest discover -s tests -t . -v  (或直接 python tests/test_table_color.py)

import os
import sys
import unittest
from datetime import datetime
from pathlib import Path

# 项目根目录:与 tests/__init__.py 的引导重复,保证两种启动方式都可用
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tigui.paths import EXE_NAME                        # noqa: E402
from tigui.ui.table import (BG_INJECTED, BG_NEW, BG_TOOL,  # noqa: E402
                            TableMixin, configure_tags,
                            is_tool_process, name_tag)


class _Var:
    """BooleanVar / StringVar 的极简替身(只用到 get/set)"""

    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Tree:
    """Treeview 的记录替身:记下标签配置与每行最终 tags"""

    def __init__(self):
        self.tag_calls = []        # [(tag, {选项})],顺序 = 优先级
        self.rows = {}             # iid -> [tag]

    def tag_configure(self, tag, **kw):
        self.tag_calls.append((tag, kw))

    def selection(self):
        return ()

    def delete(self, *_children):
        pass

    def get_children(self):
        return ()

    def insert(self, parent, index, iid=None, values=None, tags=()):
        self.rows[iid] = list(tags)

    def exists(self, _iid):
        return False

    def see(self, _iid):
        pass

    def selection_set(self, _iids):
        pass


def _target(pid, name, injectable=True, injected=False, start_time=""):
    """构造一条 list_targets 条目(字段取自 --list-targets --json 输出)"""
    return {"pid": pid, "name": name, "injectable": injectable,
            "x64": True, "console": True, "already_injected": injected,
            "reason": "", "start_time": start_time, "cmd_line": ""}


class _FakeTable(TableMixin):
    """只提供 _render_table 用到的属性;tree 用记录替身"""

    def __init__(self, targets, app_start=None):
        self.tree = _Tree()
        self.targets = targets
        self.by_pid = {t["pid"]: t for t in targets}
        self.only_injectable_var = _Var(False)
        self.sort_cid = None            # 按 PID 排,断言顺序稳定
        self.sort_reverse = False
        self.app_start = app_start
        self.sel_var = _Var()


class ToolProcessTest(unittest.TestCase):
    """工具自身识别:GUI 走 PID,注入器走进程名"""

    def test_gui_self_matched_by_pid_even_when_name_is_python(self):
        # 源码态 GUI 就是 python.exe,必须按 PID 认出来
        self.assertTrue(is_tool_process(
            _target(os.getpid(), "python.exe")))

    def test_other_python_processes_are_not_highlighted(self):
        # 同名不同 PID 的 python 不许标紫(按名字匹配就会翻车)
        self.assertFalse(is_tool_process(
            _target(os.getpid() + 1, "python.exe")))
        self.assertFalse(is_tool_process(
            _target(os.getpid() + 1, "pythonw.exe")))

    def test_injector_matched_by_name_case_insensitive(self):
        # WT 里常驻的 --mediator 与注入器是同一个 exe,同样按名字命中
        self.assertTrue(is_tool_process(_target(1234, EXE_NAME)))
        self.assertTrue(is_tool_process(_target(9999, EXE_NAME.upper())))

    def test_gui_frozen_exe_name_is_not_injector(self):
        # 连字符与下划线之差:打包后的 GUI 名不在匹配范围内(它靠 PID 命中)
        self.assertFalse(is_tool_process(
            _target(os.getpid() + 1, "terminal-injector-gui.exe")))

    def test_missing_or_empty_name_falls_back_to_pid_only(self):
        self.assertFalse(is_tool_process({"pid": os.getpid() + 1}))
        self.assertFalse(is_tool_process(
            _target(os.getpid() + 1, "")))
        self.assertTrue(is_tool_process({"pid": os.getpid(), "name": ""}))


class TagOrderTest(unittest.TestCase):
    """tag_configure 调用顺序即优先级:紫底必须最先配"""

    def setUp(self):
        self.tree = _Tree()
        configure_tags(self.tree)
        self.names = [tag for tag, _ in self.tree.tag_calls]

    def test_tool_tag_configured_first(self):
        self.assertEqual(self.names[0], "tool")

    def test_background_priority_tool_then_injected_then_new(self):
        self.assertLess(self.names.index("tool"),
                        self.names.index("injected"))
        self.assertLess(self.names.index("injected"),
                        self.names.index("new"))

    def test_foreground_priority_rejected_before_name_colors(self):
        self.assertLess(self.names.index("rejected"),
                        self.names.index(name_tag("cmd.exe")))

    def test_option_values(self):
        opts = dict(self.tree.tag_calls)
        self.assertEqual(opts["tool"], {"background": BG_TOOL})
        self.assertEqual(opts["injected"], {"background": BG_INJECTED})
        self.assertEqual(opts["new"], {"background": BG_NEW})


class RenderTagsTest(unittest.TestCase):
    """行标签选择:紫优先,绿底状态在非工具行上照常生效"""

    GUI_PID = os.getpid()

    def _render(self, targets, app_start=None):
        fake = _FakeTable(targets, app_start=app_start)
        fake._render_table()
        return fake.tree.rows

    def test_gui_self_wins_over_injected_green(self):
        rows = self._render([
            _target(self.GUI_PID, "python.exe", injected=True)])
        tags = rows[str(self.GUI_PID)]
        self.assertIn("tool", tags)
        self.assertNotIn("injected", tags, "紫底应压过标准绿")

    def test_injector_row_is_purple_even_when_not_injectable(self):
        rows = self._render([
            _target(4242, EXE_NAME, injectable=False)])
        self.assertEqual(rows["4242"], ["rejected", "tool"])

    def test_gui_self_row_is_purple_without_green(self):
        # 自身未注入(无绿底)时同样标紫
        rows = self._render([_target(self.GUI_PID, "python.exe")])
        self.assertIn("tool", rows[str(self.GUI_PID)])

    def test_plain_injected_row_keeps_green(self):
        rows = self._render([
            _target(100, "cmd.exe", injected=True),
            _target(101, "pwsh.exe")])
        self.assertIn("injected", rows["100"])
        self.assertNotIn("tool", rows["100"])
        self.assertNotIn("injected", rows["101"])
        self.assertNotIn("new", rows["101"])

    def test_new_row_keeps_light_green(self):
        app_start = datetime(2026, 1, 1, 0, 0, 0)
        rows = self._render([
            _target(200, "bash.exe", start_time="2026-09-27 12:00:00")],
            app_start=app_start)
        self.assertIn("new", rows["200"])
        self.assertNotIn("tool", rows["200"])

    def test_old_row_has_no_background_tag(self):
        app_start = datetime(2026, 9, 27, 12, 0, 0)
        rows = self._render([
            _target(300, "cmd.exe", start_time="2026-01-01 08:00:00")],
            app_start=app_start)
        self.assertEqual([t for t in rows["300"]
                          if t in ("tool", "injected", "new")], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
