# test_discover.py — discover.py / backend.discover 单元测试
#
# 覆盖:命中、深度上限、脏目录排除、junction 防环、超时、后台线程契约、
# backend 路径回填。全部用临时目录,不依赖真实二进制。
#
# 运行:python -m unittest discover -s tests -t . -v  (或直接 python tests/test_discover.py)

import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

# 项目根目录:无论 `python -m unittest discover` 还是 `python tests/xxx.py`
# 都能定位到 tigui 包(与 tests/__init__.py 的引导重复,幂等无害)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tigui import backend as backend_mod        # noqa: E402
from tigui import discover                      # noqa: E402
from tigui import i18n                          # noqa: E402
from tigui.backend import InjectorBackend       # noqa: E402
from tigui.discover import (SEARCH_MAX_DEPTH, SEARCH_TIMEOUT,  # noqa: E402
                            DiscoveryResult, find_binaries)
from tigui.paths import resolve_program_dir     # noqa: E402
from tigui.tasks import TaskRunner              # noqa: E402

EXE = "terminal_injector.exe"
DLL = "injected.dll"
JUNK_NAME = "junk_only.dll"    # 只应存在于被排除的脏目录里
DEEP_NAME = "deep_only.bin"    # 位于第 5 层目录


def setUpModule():
    # 部分断言针对中文文案;钉死语言,免得英文系统上全红
    global _LANG_PATCH
    _LANG_PATCH = mock.patch.object(i18n, "_LANG", "zh")
    _LANG_PATCH.start()


def tearDownModule():
    _LANG_PATCH.stop()


class FindBinariesTest(unittest.TestCase):
    """基础行为:命中 / 深度 / 排除 / 错误"""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="tigui_disc_"))
        (cls.root / "a" / "b").mkdir(parents=True)
        (cls.root / "a" / "b" / EXE).write_bytes(b"x")
        (cls.root / DLL).write_bytes(b"x")
        for junk in ("node_modules", "__pycache__", ".git", "logs",
                     "sample.egg-info"):
            d = cls.root / junk
            d.mkdir()
            (d / JUNK_NAME).write_bytes(b"x")
        deep = cls.root / "d1" / "d2" / "d3" / "d4" / "d5"
        deep.mkdir(parents=True)
        (deep / DEEP_NAME).write_bytes(b"x")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def find(self, names, **kw):
        kw.setdefault("root", self.root)
        kw.setdefault("timeout", 20)
        return find_binaries(names, **kw)

    def test_finds_file_in_subdirectory(self):
        res = self.find([EXE])
        self.assertIn(EXE, res.found)
        self.assertEqual(res.found[EXE].name, EXE)
        self.assertTrue(res.found[EXE].is_file())
        self.assertFalse(res.timed_out)
        self.assertEqual(res.error, "")
        # 不断言 elapsed>0:本机 time.monotonic() 粒度 15ms,
        # 小树扫描常常落在同一个时钟步长内(见 TimeoutTest)

    def test_reports_root(self):
        self.assertEqual(self.find([EXE]).root, self.root)

    def test_skips_excluded_dirs(self):
        # JUNK_NAME 散落在 5 个脏目录里,一个都不该被扫到
        res = self.find([JUNK_NAME])
        self.assertNotIn(JUNK_NAME, res.found)
        self.assertGreater(res.scanned, 0)

    def test_depth_zero_scans_only_root(self):
        res = self.find([DLL, EXE, DEEP_NAME], max_depth=0)
        self.assertIn(DLL, res.found)         # 根目录这一层
        self.assertNotIn(EXE, res.found)      # 在 a/b/ 下,不下探
        self.assertNotIn(DEEP_NAME, res.found)

    def test_depth_limit_blocks_then_allows_deep_file(self):
        # d1(1)…d5(5):扫 d4 的条目时 depth=4,须 max_depth>4 才会推入 d5
        self.assertNotIn(DEEP_NAME, self.find([DEEP_NAME], max_depth=4).found)
        self.assertIn(DEEP_NAME, self.find([DEEP_NAME], max_depth=5).found)

    def test_partial_match_returns_only_found(self):
        res = self.find([EXE, "definitely_absent.exe"])
        self.assertEqual(set(res.found), {EXE})

    def test_missing_root_reports_error(self):
        res = self.find([EXE], root=self.root / "no_such_dir")
        self.assertEqual(res.found, {})
        self.assertTrue(res.error)
        self.assertFalse(res.timed_out)
        self.assertEqual(res.scanned, 0)

    def test_default_constants_match_spec(self):
        """需求给定的默认值:深度 30 / 超时 20s"""
        self.assertEqual(SEARCH_MAX_DEPTH, 30)
        self.assertEqual(SEARCH_TIMEOUT, 20.0)

    def test_default_root_is_program_dir(self):
        """不传 root 时从 gui.py(源码态)所在目录出发"""
        res = find_binaries(["__tigui_no_such_file__"])
        self.assertEqual(res.root, resolve_program_dir())
        self.assertEqual(res.root, ROOT)

    def test_describe_limits_mentions_defaults(self):
        txt = discover.describe_limits()
        self.assertIn(str(SEARCH_MAX_DEPTH), txt)
        self.assertIn("20", txt)


class TimeoutTest(unittest.TestCase):
    """超时必须能提前收工

    本机 time.monotonic() 粒度实测约 15ms,树要大到稳定跨过一个时钟步长,
    否则 deadline 推进观测不到 —— 那是时钟粒度问题,不是实现问题,此时跳过。
    用「多目录」而非「多文件」:建树快 2.5 倍,扫描慢 3 倍。
    """

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp(prefix="tigui_disc_t_"))
        for d in range(3000):
            p = cls.root / ("d%04d" % d)
            p.mkdir()
            for i in range(5):
                (p / ("f%d.txt" % i)).write_bytes(b"x")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_timeout_stops_early(self):
        full = find_binaries(["__absent__"], root=self.root, timeout=9999)
        if full.elapsed < 0.05:
            self.skipTest("完整扫描仅 %.4fs,不足以跨过时钟步长" % full.elapsed)
        timeout = max(full.elapsed / 4, 0.02)
        res = find_binaries(["__absent__"], root=self.root, timeout=timeout)
        self.assertTrue(res.timed_out)
        self.assertLess(res.scanned, full.scanned)

    def test_no_timeout_when_plenty_of_time(self):
        res = find_binaries(["__absent__"], root=self.root, timeout=9999)
        self.assertFalse(res.timed_out)
        self.assertEqual(res.found, {})

    def test_zero_timeout_means_unlimited(self):
        res = find_binaries(["__absent__"], root=self.root, timeout=0)
        self.assertFalse(res.timed_out)
        self.assertIn("不限", discover.describe_limits(timeout=0))


class JunctionTest(unittest.TestCase):
    """重解析点(目录联接)不进入:防绕回祖先成环、防跨目录乱扫"""

    def test_junction_not_followed(self):
        if sys.platform != "win32":
            self.skipTest("仅 Windows")
        outside = Path(tempfile.mkdtemp(prefix="tigui_out_"))
        root = Path(tempfile.mkdtemp(prefix="tigui_j_"))
        try:
            (outside / EXE).write_bytes(b"x")
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(root / "link"),
                 str(outside)], capture_output=True)
            if proc.returncode != 0:
                self.skipTest("mklink 失败: "
                              + proc.stderr.decode("utf-8", "replace"))
            res = find_binaries([EXE], root=root, timeout=20)
            self.assertNotIn(EXE, res.found)
            self.assertEqual(res.scanned, 1)   # 只看了 link 这一条,没进去
        finally:
            shutil.rmtree(root, ignore_errors=True)
            shutil.rmtree(outside, ignore_errors=True)


class ExcludeTest(unittest.TestCase):
    """exclude:跳过指定目录及其整棵子树(上探第二轮排除第一轮根)"""

    NAME = "up_marker.bin"

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="tigui_ex_"))
        (self.base / "first").mkdir()
        (self.base / "first" / self.NAME).write_bytes(b"x")
        (self.base / "second" / "deep").mkdir(parents=True)
        (self.base / "second" / "deep" / self.NAME).write_bytes(b"x")

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_without_exclude_shallow_copy_wins(self):
        # 不排除时两份都在搜索范围:广度优先,first(深度 1)先于 deep(深度 2)
        res = find_binaries([self.NAME], root=self.base, timeout=20)
        self.assertEqual(res.found.get(self.NAME),
                         self.base / "first" / self.NAME)

    def test_exclude_skips_whole_subtree(self):
        res = find_binaries([self.NAME], root=self.base, timeout=20,
                            exclude=self.base / "first")
        self.assertEqual(res.found.get(self.NAME),
                         self.base / "second" / "deep" / self.NAME)

    def test_exclude_root_scans_nothing(self):
        # root 恰等于 exclude:一个条目都不该看
        res = find_binaries([self.NAME], root=self.base / "first",
                            exclude=self.base / "first")
        self.assertEqual(res.found, {})
        self.assertEqual(res.scanned, 0)


class _DummyRoot:
    """TaskRunner.poll 用到的 after;本测试只派发不轮询"""

    def after(self, *_a, **_k):
        pass


class ThreadingContractTest(unittest.TestCase):
    """搜索必须跑在后台线程,结果经队列交回主线程"""

    def _wait_queue(self, runner, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                return runner.queue.get_nowait()
            except queue.Empty:
                time.sleep(0.01)
        raise AssertionError("后台任务未在 %ss 内回传消息" % timeout)

    def test_runs_on_background_thread_via_taskrunner(self):
        seen = {}
        runner = TaskRunner(_DummyRoot(), log=lambda *a: None,
                            on_error=lambda p: seen.setdefault("err", p),
                            on_busy=lambda b: None)
        main_thread = threading.current_thread()

        def job():
            seen["thread"] = threading.current_thread()
            return find_binaries(["__absent__"], root=ROOT, timeout=20)

        runner.run_readonly(job, lambda r: seen.setdefault("result", r))
        # 回调只在主线程 poll 时触发,此刻绝不能已经跑完
        self.assertNotIn("result", seen)

        msg = self._wait_queue(runner)
        self.assertEqual(msg[0], "refresh_ok")
        self.assertNotIn("err", seen)
        self.assertIsNot(seen["thread"], main_thread)
        self.assertTrue(seen["thread"].daemon)
        msg[1](msg[2])                      # 模拟主线程 poll 派发
        self.assertIsInstance(seen["result"], DiscoveryResult)


class BackendDiscoverTest(unittest.TestCase):
    """backend.discover:只补缺失项,已就位的不覆盖"""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tigui_be_"))
        (self.root / "sub").mkdir()
        (self.root / "sub" / EXE).write_bytes(b"x")
        (self.root / DLL).write_bytes(b"x")
        # 把搜索根指到临时目录,测试才不依赖真实的二进制
        self.patcher = mock.patch.object(discover, "SEARCH_ROOT", self.root)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _backend(exe_path, dll_path):
        """构造一个路径被改写过的 backend(避免依赖 resolve_base_dir 的结果)"""
        be = InjectorBackend()
        be.exe_path = exe_path
        be.dll_path = dll_path
        return be

    def test_fills_missing_paths(self):
        be = self._backend(self.root / "gone" / EXE,
                           self.root / "gone" / DLL)
        res = be.discover(timeout=20)
        self.assertEqual(set(res.found), {EXE, DLL})
        self.assertEqual(be.exe_path, self.root / "sub" / EXE)
        self.assertEqual(be.dll_path, self.root / DLL)
        self.assertEqual(be.missing_binaries(), [])

    def test_noop_when_present(self):
        be = self._backend(self.root / "sub" / EXE, self.root / DLL)
        res = be.discover(timeout=20)
        self.assertEqual(res.found, {})
        self.assertEqual(be.missing_binaries(), [])

    def test_does_not_overwrite_present_paths(self):
        be = self._backend(self.root / "gone" / EXE,
                           self.root / "gone" / DLL)
        be.discover(timeout=20)
        keep_exe, keep_dll = be.exe_path, be.dll_path
        self.assertEqual(be.discover(timeout=20).found, {})
        self.assertEqual(be.exe_path, keep_exe)
        self.assertEqual(be.dll_path, keep_dll)


class BackendUpperSearchTest(unittest.TestCase):
    """第一轮仍缺失 -> 上探一层(父目录),排除第一轮根,超时独立"""

    FILLERS = 50        # 第一轮根里的无关文件数,用来验证第二轮没重扫它

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="tigui_up_"))
        self.first = self.base / "first"     # 第一轮根
        self.second = self.base / "second"   # 只有上探才能看到的兄弟目录
        self.first.mkdir()
        self.second.mkdir()
        for i in range(self.FILLERS):
            (self.first / ("filler%d.txt" % i)).write_bytes(b"x")
        (self.second / EXE).write_bytes(b"x")
        (self.second / DLL).write_bytes(b"x")
        self.patcher = mock.patch.object(discover, "SEARCH_ROOT", self.first)
        self.patcher.start()
        self.logs = []                       # backend 日志留痕

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def _backend(self, exe=None, dll=None):
        be = InjectorBackend()
        be.exe_path = exe or self.first / "gone" / EXE
        be.dll_path = dll or self.first / "gone" / DLL
        be.set_log_sink(lambda text, tag="info": self.logs.append((tag, text)))
        return be

    def test_upper_round_finds_and_reports(self):
        be = self._backend()
        res = be.discover(timeout=20)
        self.assertEqual(set(res.found), {EXE, DLL})
        self.assertEqual(be.exe_path, self.second / EXE)
        self.assertEqual(be.dll_path, self.second / DLL)
        self.assertEqual(be.missing_binaries(), [])
        self.assertIsNotNone(res.parent_round, "缺失时应记上探那一轮")
        self.assertEqual(res.parent_round.root, self.base)
        self.assertFalse(res.parent_round.timed_out)
        # 上探开工要留痕:带上层根、第一轮根与限制说明
        start = [t for _, t in self.logs if "上探父目录" in t]
        self.assertEqual(len(start), 1, str(self.logs))
        self.assertIn(str(self.base), start[0])
        self.assertIn(str(self.first), start[0])

    def test_upper_round_does_not_rescan_first_root(self):
        res = self._backend().discover(timeout=20)
        # 排除生效时第二轮只看 base(2 条)+ second(2 条);
        # 若没排除 first,那 50 个 filler 会被数进去(~54)
        self.assertLess(res.parent_round.scanned, 20,
                        "上探不得重复扫描第一轮根子树")

    def test_partial_find_still_goes_up(self):
        (self.first / EXE).write_bytes(b"x")     # 第一轮能补上 exe
        be = self._backend()
        res = be.discover(timeout=20)
        self.assertEqual(set(res.found), {EXE, DLL})
        self.assertEqual(be.exe_path, self.first / EXE, "已命中的不被覆盖")
        self.assertEqual(be.dll_path, self.second / DLL)
        self.assertIsNotNone(res.parent_round, "只缺一样也要上探")

    def test_upper_round_error_keeps_first_round_result(self):
        (self.first / EXE).write_bytes(b"x")     # 第一轮就能找到 exe
        real = backend_mod.find_binaries
        calls = []

        def flaky(names, **kw):
            calls.append(kw)
            if len(calls) == 1:
                return real(names, **kw)         # 第一轮用真搜索
            return DiscoveryResult({}, kw["root"], error="父目录刚消失")

        with mock.patch.object(backend_mod, "find_binaries", flaky):
            be = self._backend()
            res = be.discover(timeout=20)
        self.assertEqual(len(calls), 2, "仍缺失 -> 应确实起第二轮")
        self.assertEqual(res.error, "", "第二轮的 error 不并进总结果")
        self.assertEqual(set(res.found), {EXE}, "第一轮的结果要保住")
        self.assertEqual(be.exe_path, self.first / EXE)
        self.assertEqual(be.missing_binaries(), [DLL])
        self.assertIsNotNone(res.parent_round)
        self.assertTrue(any("父目录刚消失" in t for _, t in self.logs),
                        str(self.logs))

    def test_no_upper_round_when_all_present(self):
        be = self._backend(self.second / EXE, self.second / DLL)
        res = be.discover(timeout=20)
        self.assertEqual(res.found, {})
        self.assertIsNone(res.parent_round)
        self.assertEqual(self.logs, [], "不缺失就不该有搜索动作")

    def test_skip_when_root_is_drive_root(self):
        res = DiscoveryResult({}, Path(Path(self.base).anchor))  # 如 C:\
        out = self._backend()._search_upper(res, [EXE], 30, 20)
        self.assertIsNone(out.parent_round, "已是根目录,不该上探")

    def test_skip_when_parent_missing(self):
        res = DiscoveryResult({}, self.base / "no_such" / "leaf")
        out = self._backend()._search_upper(res, [EXE], 30, 20)
        self.assertIsNone(out.parent_round, "父目录不存在时原样返回")


if __name__ == "__main__":
    unittest.main(verbosity=2)
