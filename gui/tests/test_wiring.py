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

from tigui import app as app_mod                 # noqa: E402
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


class _BoolVar:
    """auto_refresh_var 的替身:tk.BooleanVar 在 _auto_refresh_loop 里只用 get()"""

    def __init__(self, value=True):
        self.value = value

    def get(self):
        return self.value


class Fake:
    """只提供 rediscover / _discovery_job / _on_discovered / refresh 用到的属性"""

    def __init__(self):
        self.logs = []                     # [(tag, text)]
        self.backend = InjectorBackend()
        self.tasks = TaskRunner(_DummyRoot(), log=self._sink,
                                on_error=lambda p: self.logs.append(("ERR", p)),
                                on_busy=lambda b: None)
        self._discovering = False
        self.refresh_calls = 0
        self.version_calls = 0
        self.auto_refresh_var = _BoolVar(True)
        self.after_calls = 0               # _auto_refresh_loop 自我排程次数

    # --- LogMixin / app 的替身 ---
    def _sink(self, text, tag="info"):
        self.logs.append((tag, text))

    def log(self, text, tag="info"):
        self.logs.append((tag, text))

    def _load_version(self):
        self.version_calls += 1

    def _on_targets(self, targets):
        self.refresh_calls += 1          # 真实 refresh 派发出去才算刷到

    def after(self, *_a, **_k):
        self.after_calls += 1             # 只记数,不真排程

    # --- 委托给真实的实现 ---
    # refresh 保持计数器语义(既有断言靠它);守卫本身用 InjectorGui.refresh
    # unbound 直调验证,见 RefreshGuardTest。
    def refresh(self):
        self.refresh_calls += 1

    def _discovery_job(self):
        return InjectorGui._discovery_job(self)

    def _on_discovered(self, res):
        InjectorGui._on_discovered(self, res)

    def _auto_refresh_loop(self):
        InjectorGui._auto_refresh_loop(self)

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

        # _on_discovered 的「仍缺失」分支会弹 messagebox;测试里换成记录桩,
        # 免得真开对话框(也便于断言弹/不弹)
        mb = mock.patch.object(app_mod, "messagebox")
        self.mb = mb.start()
        self.addCleanup(mb.stop)

        self.app = Fake()
        self._set_paths(missing=True)

    def popped(self):
        """已弹出的警告窗:[(title, message), ...]"""
        return [(c.args[0], c.args[1]) for c in self.mb.showwarning.call_args_list]

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
        self.assertEqual(self.popped(), [], "就位时不该弹窗")

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
        self.assertEqual(self.popped(), [], "补齐就不该再弹窗")

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
        # 本目录搜完仍缺失 -> 弹窗,文案列全缺失名
        self.assertEqual(len(self.popped()), 1, str(self.popped()))
        self.assertIn(EXE, self.popped()[0][1])
        self.assertIn(DLL, self.popped()[0][1])

    def test_two_rounds_still_missing_pops_dialog(self):
        # 第一轮 + 上探父目录两轮都空手 -> 同样要弹
        upper = Path("/tmp/upper")
        self.app._discovery_job = lambda: DiscoveryResult(
            {}, discover.current_root(), 0.5, 42, False, "",
            SearchRound(upper, 7, False, 0.2))
        InjectorGui.rediscover(self.app)
        self.pump()
        self.assertEqual(len(self.popped()), 1, str(self.popped()))
        self.assertIn(EXE, self.popped()[0][1])

    def test_partial_hit_still_pops_for_leftover(self):
        # 只补上 dll、仍缺 exe:按「仍缺失」处理,照样弹,且只列缺的那一样
        self.app.backend.dll_path = self.root / DLL   # dll 已就位
        self.app._discovery_job = lambda: DiscoveryResult(
            {DLL: self.root / DLL}, discover.current_root(),
            0.5, 42, False, "")
        InjectorGui.rediscover(self.app)
        self.pump()
        self.assertEqual(len(self.popped()), 1)
        # 首行是「未找到: ...」清单,只该列缺的 exe;后两行的文件名提示照旧
        head = self.popped()[0][1].splitlines()[0]
        self.assertIn(EXE, head)
        self.assertNotIn(DLL, head)

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
        self.assertEqual(self.popped(), [], "error 分支不弹窗(没真搜过)")

    def test_error_result_reports_error(self):
        self.app._discovery_job = lambda: DiscoveryResult(
            {}, discover.current_root(), 0.0, 0, False, "坏根目录")
        InjectorGui.rediscover(self.app)
        self.pump()
        self.assertFalse(self.app._discovering)
        self.assertTrue(any("坏根目录" in t for t in self.app.lines("err")))
        self.assertEqual(self.app.refresh_calls, 0)
        self.assertEqual(self.popped(), [], "error 分支不弹窗(没真搜过)")


class RefreshGuardTest(unittest.TestCase):
    """exe 缺失时 refresh 不得起子进程

    subprocess 必抛 FileNotFoundError -> refresh_err -> _on_task_error 弹
    showerror;启动(无条件 refresh)与 3 秒自动刷新叠起来就是连环弹窗。
    用户决策:不弹窗,记一条 error 日志即可。
    """

    def setUp(self):
        self.app = Fake()

    @staticmethod
    def _missing_exe():
        return Path(r"C:\__tigui_no_such_dir__") / EXE

    @staticmethod
    def _wait_message(runner, timeout=10.0):
        """等 worker 线程回包(_submit 是异步的,派发瞬间队列必然还是空的)"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return runner.queue.get_nowait()
            except queue.Empty:
                time.sleep(0.01)
        raise AssertionError("派发后 %ss 内未回包" % timeout)

    def test_missing_exe_skips_dispatch_and_logs_error(self):
        self.app.backend.exe_path = self._missing_exe()
        InjectorGui.refresh(self.app)
        self.assertTrue(self.app.tasks.queue.empty(),
                        "缺 exe 不得起子进程(否则必然走 showerror)")
        errs = self.app.lines("err")
        self.assertEqual(len(errs), 1, str(self.app.logs))
        self.assertIn(EXE, errs[0])

    def test_present_exe_dispatches_readonly(self):
        # exe_path 指向本测试文件:存在性过关就会派发;它不是合法 PE,worker
        # 必失败 -> refresh_err。断言前缀即可证明「派发了」与「只读不占 busy」
        self.app.backend.exe_path = Path(__file__)
        InjectorGui.refresh(self.app)
        kind, _cb, _payload = self._wait_message(self.app.tasks)
        self.assertEqual(kind, "refresh_err")
        self.assertFalse(self.app.tasks.busy, "只读任务不得占用 busy")

    def test_auto_refresh_silent_when_exe_missing(self):
        # 守卫在 refresh() 之前:循环自身不调 refresh,也就不再每 3 秒
        # 刷一条同类 error;启动时 __init__ 直调的那一条已经够了
        self.app.backend.exe_path = self._missing_exe()
        InjectorGui._auto_refresh_loop(self.app)
        self.assertEqual(self.app.refresh_calls, 0)
        self.assertTrue(self.app.tasks.queue.empty())
        self.assertEqual(self.app.lines("err"), [], "自动刷新不应反复刷日志")
        self.assertEqual(self.app.after_calls, 1, "轮询链必须继续排程")

    def test_auto_refresh_dispatches_when_exe_present(self):
        # Fake.refresh 是计数器桩:这里只验证循环调了它;真实派发由
        # test_present_exe_dispatches_readonly 覆盖(直接 unbound 调 refresh)
        self.app.backend.exe_path = Path(__file__)
        InjectorGui._auto_refresh_loop(self.app)
        self.assertEqual(self.app.refresh_calls, 1)
        self.assertEqual(self.app.after_calls, 1)

    def test_auto_refresh_respects_toggle(self):
        self.app.auto_refresh_var.value = False
        self.app.backend.exe_path = Path(__file__)
        InjectorGui._auto_refresh_loop(self.app)
        self.assertEqual(self.app.refresh_calls, 0, "关掉自动刷新就不该调")
        self.assertTrue(self.app.tasks.queue.empty())
        self.assertEqual(self.app.after_calls, 1, "关掉也要继续排程")


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
