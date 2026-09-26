# test_wiring.py — app.rediscover / _on_discovered 接线测试
#
# 用 unbound 方式调用 InjectorGui 的方法 + Fake self,因而:
#   - 不创建 Tk 窗口、不弹对话框、不开真实主循环
#   - 不依赖真实 terminal_injector.exe(搜索根被 patch 到临时目录)
#   - 不会真的注入任何进程
#
# 运行:python -m unittest discover -s tests -t . -v  (或直接 python tests/test_wiring.py)

import queue
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# 项目根目录:与 tests/__init__.py 的引导重复,保证两种启动方式都可用
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tigui import discover                      # noqa: E402
from tigui import i18n                          # noqa: E402
from tigui.app import InjectorGui               # noqa: E402
from tigui.backend import InjectorBackend       # noqa: E402
from tigui.discover import DiscoveryResult, SearchRound   # noqa: E402
from tigui.paths import resolve_program_dir     # noqa: E402
from tigui.tasks import TaskRunner              # noqa: E402

EXE = "terminal_injector.exe"
DLL = "injected.dll"


def setUpModule():
    # 断言针对中文文案;钉死语言,免得英文系统上全红
    global _LANG_PATCH
    _LANG_PATCH = mock.patch.object(i18n, "_LANG", "zh")
    _LANG_PATCH.start()


def tearDownModule():
    _LANG_PATCH.stop()


class _DummyRoot:
    """TaskRunner.poll 用到的 after;测试里不轮询,由 pump 手工派发"""

    def after(self, *_a, **_k):
        pass


class Fake:
    """只提供 rediscover / _discovery_job / _on_discovered 用到的属性"""

    def __init__(self):
        self.logs = []                     # [(tag, text)]
        self.backend = InjectorBackend()
        self.tasks = TaskRunner(_DummyRoot(), log=self._sink,
                                on_error=lambda p: self.logs.append(("ERR", p)),
                                on_busy=lambda b: None)
        self._discovering = False
        self.refresh_calls = 0
        self.version_calls = 0

    # --- LogMixin / app 的替身 ---
    def _sink(self, text, tag="info"):
        self.logs.append((tag, text))

    def log(self, text, tag="info"):
        self.logs.append((tag, text))

    def refresh(self):
        self.refresh_calls += 1

    def _load_version(self):
        self.version_calls += 1

    # --- 委托给真实的实现 ---
    def _discovery_job(self):
        return InjectorGui._discovery_job(self)

    def _on_discovered(self, res):
        InjectorGui._on_discovered(self, res)

    # --- 断言辅助 ---
    def lines(self, tag=None):
        return [t for g, t in self.logs if tag is None or g == tag]


class RediscoverTest(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tigui_wire_"))
        (self.root / "sub").mkdir()
        (self.root / "sub" / EXE).write_bytes(b"x")
        (self.root / DLL).write_bytes(b"x")
        # 搜索根指到临时目录:测试不依赖用户是否真有那两个二进制
        patcher = mock.patch.object(discover, "SEARCH_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.root, True)

        self.app = Fake()
        self._set_paths(missing=True)

    def _set_paths(self, missing=True):
        if missing:
            self.app.backend.exe_path = self.root / "gone" / EXE
            self.app.backend.dll_path = self.root / "gone" / DLL
        else:
            self.app.backend.exe_path = self.root / "sub" / EXE
            self.app.backend.dll_path = self.root / DLL

    def pump(self, timeout=30.0):
        """模拟主线程 poll:等后台线程写入队列并派发回调"""
        deadline = time.monotonic() + timeout
        got = []
        while time.monotonic() < deadline:
            try:
                kind, cb, payload = self.app.tasks.queue.get_nowait()
            except queue.Empty:
                if got:
                    return got
                time.sleep(0.01)
                continue
            got.append(kind)
            if kind in ("ok", "refresh_ok"):
                cb(payload)
            elif kind in ("err", "refresh_err"):
                self.app.logs.append(("ERR", payload))
        self.fail("后台任务未在 %ss 内回传消息" % timeout)

    # ---------- 用例 ----------

    def test_noop_when_all_present(self):
        self._set_paths(missing=False)
        InjectorGui.rediscover(self.app)
        self.assertFalse(self.app._discovering)
        self.assertTrue(self.app.tasks.queue.empty())
        self.assertTrue(any("无需探测" in t for t in self.app.lines()))

    def test_missing_starts_background_search_and_fills(self):
        InjectorGui.rediscover(self.app)
        self.assertTrue(self.app._discovering, "派发后应处于搜索中")
        start = next(t for t in self.app.lines() if "根目录" in t)
        self.assertIn(str(self.root), start, "启动日志必须报真实搜索根")
        self.assertIn(discover.describe_limits(), start, "启动日志必须带限制说明")

        self.assertEqual(self.pump(), ["refresh_ok"])
        self.assertFalse(self.app._discovering)
        self.assertEqual(self.app.backend.exe_path, self.root / "sub" / EXE)
        self.assertEqual(self.app.backend.dll_path, self.root / DLL)
        self.assertEqual(self.app.backend.missing_binaries(), [])
        self.assertEqual(len(self.app.lines("ok")), 2)   # 两条"找到"
        self.assertEqual(self.app.refresh_calls, 1, "补齐后应刷新列表")
        self.assertEqual(self.app.version_calls, 1, "补齐后应重取版本")

    def test_busy_guard_blocks_repeat_triggers(self):
        for _ in range(3):
            InjectorGui.rediscover(self.app)
        starts = [t for t in self.app.lines() if "根目录" in t]
        blocked = [t for t in self.app.lines() if "已在进行" in t]
        self.assertEqual(len(starts), 1, "只应起一次搜索")
        self.assertEqual(len(blocked), 2, "后两次应被拦下")
        self.pump()   # 清场

    def test_not_found_reports_without_refresh(self):
        self.app._discovery_job = lambda: DiscoveryResult(
            {}, discover.current_root(), 0.5, 42, False, "")
        InjectorGui.rediscover(self.app)
        self.pump()
        self.assertFalse(self.app._discovering)
        self.assertTrue(any("未找到" in t for t in self.app.lines("err")))
        self.assertEqual(self.app.refresh_calls, 0, "没找到就不该刷新")
        self.assertEqual(self.app.backend.missing_binaries(), [EXE, DLL])

    def test_upper_round_report_mentions_both_roots(self):
        # 上探过的失败报告:两个搜索根都要写明,扫描数为两轮之和
        upper = Path("/tmp/upper")
        self.app._discovery_job = lambda: DiscoveryResult(
            {}, discover.current_root(), 0.5, 42, False, "",
            SearchRound(upper, 7, False, 0.2))
        InjectorGui.rediscover(self.app)
        self.pump()
        errs = self.app.lines("err")
        line = next((t for t in errs if "上层" in t), None)
        self.assertIsNotNone(line, str(self.app.logs))
        self.assertIn("未找到", line)
        self.assertIn(str(discover.current_root()), line)
        self.assertIn(str(upper), line)
        self.assertIn("42", line, "扫描数应为两轮之和")
        self.assertEqual(self.app.refresh_calls, 0)

    def test_exception_folds_to_error(self):
        def boom(**_kw):
            raise RuntimeError("boom")

        self.app.backend.discover = boom
        InjectorGui.rediscover(self.app)
        self.pump()
        self.assertFalse(self.app._discovering, "异常也不能卡死标志")
        self.assertTrue(any("搜索失败" in t and "boom" in t
                            for t in self.app.lines("err")),
                        str(self.app.logs))

    def test_error_result_reports_error(self):
        self.app._discovery_job = lambda: DiscoveryResult(
            {}, discover.current_root(), 0.0, 0, False, "坏根目录")
        InjectorGui.rediscover(self.app)
        self.pump()
        self.assertFalse(self.app._discovering)
        self.assertTrue(any("坏根目录" in t for t in self.app.lines("err")))
        self.assertEqual(self.app.refresh_calls, 0)


class SearchRootTest(unittest.TestCase):
    """源码态的搜索根必须是 gui.py 所在目录,与入口脚本是谁无关"""

    def test_default_root_is_program_dir(self):
        self.assertEqual(resolve_program_dir(), ROOT)

    def test_current_root_follows_search_root_override(self):
        with mock.patch.object(discover, "SEARCH_ROOT", Path("/tmp/x")):
            self.assertEqual(discover.current_root(), Path("/tmp/x"))
        self.assertEqual(discover.current_root(), resolve_program_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
