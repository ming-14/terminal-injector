"""e2e 统一测试 runner。

扫描 <root>/<类别>/test_*.py，逐文件独立进程运行（隔离崩溃与残留），
解析各文件 SUMMARY 输出，汇总 PASS/FAIL/UNSUPPORTED 报告。

用法：
  python run_all.py                  # 运行全部测试
  python run_all.py --list           # 列出所有测试
  python run_all.py --cat mouse      # 运行指定类别目录
  python run_all.py --phase 1        # 按 PHASES.md 的阶段（1-15）
  python run_all.py --file mouse/test_mouse_click.py   # 单个文件
  python run_all.py --help

每个测试文件亦可单独运行：python mouse/test_mouse_click.py
"""
import argparse
import importlib.util
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 统一 UTF-8 输出，避免 GBK 控制台编码问题
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from common import paths
from common import reporter

# PHASES.md 阶段 → 类别目录映射（阶段划分见 docs/PHASES.md）
PHASE_CATS = {
    0: [],                                   # 基建（无测试文件）
    1: ["vt_output"],
    2: ["console_api"],
    3: ["cursor_buffer"],
    4: ["keyboard"],
    5: ["line_editor"],
    6: ["modes"],
    7: ["vt_passthrough"],
    8: ["mouse"],
    9: ["special_sequences"],
    10: ["codepage"],
    11: ["width"],
    12: ["scrollback"],
    13: ["lifecycle"],
    14: ["performance"],
    15: [],                                  # 全量回归
}


def discover_tests() -> list:
    """返回 [(类别, 文件名, 绝对路径)]，按类别排序。"""
    tests = []
    for entry in sorted(os.listdir(paths.TESTS_ALL_ROOT)):
        cat_dir = os.path.join(paths.TESTS_ALL_ROOT, entry)
        if not os.path.isdir(cat_dir) or entry.startswith("_") or entry in ("docs", "common", "helpers"):
            continue
        for f in sorted(os.listdir(cat_dir)):
            if f.startswith("test_") and f.endswith(".py"):
                tests.append((entry, f, os.path.join(cat_dir, f)))
    return tests


def list_tests() -> None:
    tests = discover_tests()
    print("共 {} 个测试文件：".format(len(tests)))
    for cat, f, _ in tests:
        print("  [{:16s}] {}".format(cat, f))


def run_file(path: str) -> reporter.Summary:
    """独立进程运行单个测试文件，返回 Summary。"""
    name = os.path.splitext(os.path.basename(path))[0]
    print("\n" + "=" * 60)
    print("运行: {}".format(os.path.relpath(path, paths.TESTS_ALL_ROOT)))
    print("=" * 60)
    t0 = time.time()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, path],
            capture_output=True, text=True, env=env, timeout=600,
            encoding="utf-8", errors="replace",
        )
        exit_code = proc.returncode
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        exit_code = 1
        out = "[TIMEOUT] 测试超过 600s"
    print(out)
    print("耗时 {:.1f}s".format(time.time() - t0))
    return reporter.Summary.parse(name, out, exit_code)


def main() -> int:
    ap = argparse.ArgumentParser(description="e2e 统一 runner")
    ap.add_argument("--list", action="store_true", help="列出所有测试")
    ap.add_argument("--cat", nargs="+", help="运行指定类别目录")
    ap.add_argument("--phase", type=int, choices=range(0, 16), help="运行指定阶段（0-15）")
    ap.add_argument("--file", nargs="+", help="运行指定文件（相对 e2e 根）")
    args = ap.parse_args()

    if args.list:
        list_tests()
        return 0

    paths.ensure_dirs()

    all_tests = discover_tests()
    selected = []

    if args.phase is not None:
        cats = PHASE_CATS.get(args.phase, [])
        if args.phase == 0:
            print("Phase 0 为基建阶段，无测试文件。运行 --cat vt_output 验证。")
            return 0
        if args.phase == 15:
            selected = list(all_tests)
        else:
            selected = [t for t in all_tests if t[0] in cats]
        label = "阶段 {}".format(args.phase)
    elif args.cat:
        selected = [t for t in all_tests if t[0] in args.cat]
        label = "类别 {}".format(",".join(args.cat))
    elif args.file:
        for f in args.file:
            path = os.path.normpath(os.path.join(paths.TESTS_ALL_ROOT, f))
            if not os.path.exists(path):
                print("[ERROR] 文件不存在: {}".format(path))
                return 2
            name = os.path.splitext(os.path.basename(path))[0]
            cat = os.path.basename(os.path.dirname(path))
            selected.append((cat, os.path.basename(path), path))
        label = "指定文件"
    else:
        selected = list(all_tests)
        label = "全部"

    if not selected:
        print("没有匹配的测试文件。用 --list 查看。")
        return 2

    print("运行[{}]: {} 个测试".format(label, len(selected)))

    report = reporter.Report()
    for cat, f, path in selected:
        s = run_file(path)
        report.add(s)
        time.sleep(1.0)

    report.print_table()
    summary_path = os.path.join(paths.RESULTS_DIR, "summary.json")
    report.write_json(summary_path)
    print("汇总报告: {}".format(summary_path))

    return 1 if report.total_failures else 0


if __name__ == "__main__":
    sys.exit(main())
