"""e2e_v2 运行器。

扫描 <root>/<类别>/test_*.py，逐文件独立进程运行（隔离崩溃与残留），
解析各文件的 SUMMARY 行汇总。

用法（在 tests/e2e_v2 目录下）：
  python run_all.py                    # 全量
  python run_all.py --list             # 列出
  python run_all.py --cat lifecycle    # 指定类别
  python run_all.py --file lifecycle/test_inject_handshake.py
  python run_all.py --preflight        # 只检查前提（产物 / pywezterm）

退出码：0 = 无失败用例（UNSUPPORTED 不算失败）；1 = 存在 FAIL 或 ERROR；2 = 参数错误。
"""
import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pwcommon import paths
from pwcommon import reporter

# 非类别目录（不参与发现）
SKIP_DIRS = ("pwcommon", "_results", "_targets", "docs")


def discover_tests() -> list:
    """返回 [(类别, 文件名, 绝对路径)]。"""
    tests = []
    for entry in sorted(os.listdir(paths.V2_ROOT)):
        if entry.startswith("_") or entry in SKIP_DIRS:
            continue
        cat_dir = os.path.join(paths.V2_ROOT, entry)
        if not os.path.isdir(cat_dir):
            continue
        for f in sorted(os.listdir(cat_dir)):
            if f.startswith("test_") and f.endswith(".py"):
                tests.append((entry, f, os.path.join(cat_dir, f)))
    return tests


def run_file(path: str) -> reporter.Summary:
    name = os.path.splitext(os.path.basename(path))[0]
    print("\n" + "=" * 72)
    print("运行: {}  ({})".format(os.path.relpath(path, paths.V2_ROOT), name))
    print("=" * 72)
    t0 = time.time()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True,
                              env=env, timeout=600, encoding="utf-8", errors="replace")
        exit_code = proc.returncode
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired:
        exit_code = 1
        out = "[TIMEOUT] 用例超过 600s"
    print(out)
    print("耗时 {:.1f}s".format(time.time() - t0))
    return reporter.Summary.parse(name, out, exit_code)


def main() -> int:
    ap = argparse.ArgumentParser(description="e2e_v2 运行器")
    ap.add_argument("--list", action="store_true", help="列出全部用例")
    ap.add_argument("--cat", nargs="+", help="只跑指定类别目录")
    ap.add_argument("--file", nargs="+", help="只跑指定文件（相对 e2e_v2 根）")
    ap.add_argument("--preflight", action="store_true", help="只检查运行前提")
    args = ap.parse_args()

    if args.preflight:
        missing = paths.preflight()
        print("pywezterm 目录: {}".format(paths.pywezterm_dir()))
        print("构建产物目录: {}".format(paths.BUILD_BIN))
        if missing:
            for m in missing:
                print("  [缺失] {}".format(m))
            return 1
        print("  [OK] 前提齐备")
        return 0

    if args.list:
        tests = discover_tests()
        print("共 {} 个用例：".format(len(tests)))
        for cat, f, _ in tests:
            print("  [{:16s}] {}".format(cat, f))
        return 0

    paths.ensure_dirs()
    all_tests = discover_tests()
    selected = []
    if args.cat:
        selected = [t for t in all_tests if t[0] in args.cat]
        label = "类别 {}".format(",".join(args.cat))
    elif args.file:
        for f in args.file:
            path = os.path.normpath(os.path.join(paths.V2_ROOT, f))
            if not os.path.exists(path):
                print("[ERROR] 文件不存在: {}".format(path))
                return 2
            selected.append((os.path.basename(os.path.dirname(path)),
                             os.path.basename(path), path))
        label = "指定文件"
    else:
        selected = list(all_tests)
        label = "全部"

    if not selected:
        print("没有匹配的用例（用 --list 查看）。")
        return 2

    print("运行[{}]: {} 个用例".format(label, len(selected)))
    t0 = time.time()
    report = reporter.Report()
    for _, _, path in selected:
        report.add(run_file(path))
        time.sleep(0.5)

    report.print_table(time.time() - t0)
    summary_path = os.path.join(paths.RESULTS_DIR, "summary.json")
    report.write_json(summary_path)
    print("汇总报告: {}".format(summary_path))
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
