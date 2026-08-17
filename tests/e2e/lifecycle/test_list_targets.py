"""特性: --list-targets 进程枚举    类别: lifecycle

链路: 本地 CLI（不依赖注入/WT 链路）：
      terminal_injector.exe --list-targets [--json] [--all]

预期:
  - 默认只输出可注入进程（STATUS 均为 injectable）
  - --all 附带不可注入进程及原因标记（access_denied / not_console 等）
  - --json 输出合法 JSON，且默认同样只含可注入项
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common import paths

NAME = "list_targets"

EXE = os.path.join(paths.BUILD_BIN, "terminal_injector.exe")


def run_cli(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [EXE, "--list-targets", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=60,
    )


def run() -> int:
    failures = 0

    if not os.path.isfile(EXE):
        print("  [FAIL] 未找到 {}（先构建）".format(EXE))
        failures += 1
        print("\nSUMMARY: FAIL ({} failures)".format(failures))
        return failures

    # 1. 默认输出：仅可注入项
    p = run_cli()
    if p.returncode != 0:
        print("  [FAIL] --list-targets 退出码 {}（期望 0）".format(p.returncode))
        failures += 1
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    header = lines[0] if lines else ""
    rows = lines[1:]
    print("  [INFO] 默认列出 {} 个进程".format(len(rows)))
    if header.split("\t") != ["PID", "NAME", "STATUS"]:
        print("  [FAIL] 表头异常: {}".format(header))
        failures += 1
    bad = [r for r in rows if "\tinjectable" not in r]
    if bad:
        print("  [FAIL] 默认输出含不可注入行: {}".format(bad[:3]))
        failures += 1
    else:
        print("  [PASS] 默认仅输出可注入进程（{} 个）".format(len(rows)))

    # 2. --all：附带不可注入进程及原因
    pa = run_cli("--all")
    rows_a = pa.stdout.splitlines()[1:]
    print("  [INFO] --all 列出 {} 个进程".format(len(rows_a)))
    if len(rows_a) <= len(rows):
        print("  [FAIL] --all 行数（{}）未多于默认（{}）".format(
            len(rows_a), len(rows)))
        failures += 1
    non_ok = [r for r in rows_a if "\tinjectable" not in r]
    if not non_ok:
        print("  [FAIL] --all 未见不可注入进程（原因标记缺失）")
        failures += 1
    else:
        sample = non_ok[0].split("\t")[2]
        print("  [PASS] --all 附带 {} 个不可注入进程（如: {}）".format(
            len(non_ok), sample))

    # 3. --json：合法 JSON，默认仅 injectable=true
    pj = run_cli("--json")
    try:
        arr = json.loads(pj.stdout)
    except json.JSONDecodeError as e:
        print("  [FAIL] --json 输出非法: {}".format(e))
        arr = []
        failures += 1
    if arr and all(t.get("injectable") for t in arr):
        print("  [PASS] --json 输出 {} 条，全部 injectable=true".format(len(arr)))
    else:
        print("  [FAIL] --json 应仅含 injectable=true（{} 条）".format(len(arr)))
        failures += 1

    # 4. 可注入项应能对应真实进程（当前测试进程本身是控制台程序）
    self_pid = os.getpid()
    pids = [int(r.split("\t")[0]) for r in rows]
    if self_pid in pids:
        print("  [PASS] 当前测试进程（python, PID={}）在可注入列表".format(self_pid))
    else:
        print("  [FAIL] 当前测试进程（PID={}）未在可注入列表".format(self_pid))
        failures += 1

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if failures == 0 else "FAIL", failures))
    return failures


if __name__ == "__main__":
    sys.exit(run())
