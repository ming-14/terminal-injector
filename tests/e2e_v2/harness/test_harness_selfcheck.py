"""特性: 脚手架自检（命令行引号 / 按键修饰 / 结果汇总判定）    类别: harness

不需要注入、不起终端，秒级完成。钉住三处**静默错误**类的坑（不报错但做错事，
普通 e2e 用例只会表现为"偶发失败"，很难归因）：

1. **命令行引号**：launcher 路径带空格（`C:\\Program Files\\...\\python.exe`）不加引号
   会被 cmd 按空格切开，报 `'C:\\Program' 不是内部或外部命令`。
   这里不只是拼字符串断言，而是**真的丢给 cmd /c 跑一遍**验证引号有效。
2. **按键修饰**：pywezterm 的 `key_down` 不认 `"ctrl-c"` 这种写法，会当单字符处理
   返回 `b'c'` —— Ctrl 被静默丢掉。必须走 `parse_key` 翻成 mods 位。
3. **结果汇总判定**：v1 的 `Summary` 默认 PASS（零输出退出的用例被判过）、
   且 `total_failures` 把 FAIL 项重复计数。v2 的判定矩阵固化在这里。

另外验证 `parse_launcher` 不吞 Windows 路径的反斜杠（`shlex` 的 POSIX 规则会吞）。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pwcommon import paths
from pwcommon import reporter
from pwcommon import pyterm
from pwcommon.session import (MOD_ALT, MOD_CTRL, MOD_SHIFT, build_command,
                              parse_key, parse_launcher)

NAME = "harness_selfcheck"


def _check(failures: list, label: str, actual, expected):
    if actual == expected:
        print("  [PASS] {} = {!r}".format(label, actual))
    else:
        print("  [FAIL] {} = {!r}，期望 {!r}".format(label, actual, expected))
        failures.append(label)


def run() -> int:
    pw = pyterm.require()      # 需要 Terminal 来验证按键编码
    failures = []

    # ── 1. 命令行引号：真的丢给 cmd 跑一遍
    print("\n── 命令行引号 ──")
    space_dir = os.path.join(paths.RESULTS_DIR, "_harness dir")
    os.makedirs(space_dir, exist_ok=True)
    script = os.path.join(space_dir, "引号 测试.py")
    result = os.path.join(space_dir, "结 果.txt")
    with open(script, "w", encoding="utf-8") as f:
        f.write("import sys\nprint('QUOTE_OK', sys.argv[1])\n")

    cmd = build_command(parse_launcher(None), script, result)
    print("  [INFO] 生成的命令行: {}".format(cmd))
    # 必须用 shell=True：真实形态是"把这串字符敲进 shell"，由 cmd 自己解析引号。
    # 若走 argv 列表，Python 会再套一层引号（变成 cmd /c "\"C:\Program ...\""）→ 误判。
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30,
                         encoding="utf-8", errors="replace")
    blob = (out.stdout or "") + (out.stderr or "")
    if "QUOTE_OK" in blob:
        print("  [PASS] 带空格的路径经 cmd 解析后正确执行（引号有效）")
    else:
        print("  [FAIL] 带空格路径未能正确执行，实收: {!r}".format(blob[:200]))
        failures.append("命令行引号")

    _check(failures, "parse_launcher(None)", parse_launcher(None), [sys.executable])
    _check(failures, "parse_launcher(存在的路径)", parse_launcher(script), [script])
    _check(failures, "parse_launcher('py -3')", parse_launcher("py -3"), ["py", "-3"])
    _check(failures, "parse_launcher(不存在的 Windows 路径，反斜杠必须保留)",
           parse_launcher(r"C:\no\such\exe.exe -3"),
           ["C:\\no\\such\\exe.exe", "-3"])

    # ── 2. 按键修饰：parse_key 必须翻成 mods，编码必须真的是 Ctrl-C
    print("\n── 按键修饰 ──")
    _check(failures, "parse_key('ctrl-c')", parse_key("ctrl-c"), ("c", MOD_CTRL))
    _check(failures, "parse_key('alt-Enter')", parse_key("alt-Enter"), ("Enter", MOD_ALT))
    _check(failures, "parse_key('shift-Tab')", parse_key("shift-Tab"), ("Tab", MOD_SHIFT))
    _check(failures, "parse_key('ctrl-shift-a')",
           parse_key("ctrl-shift-a"), ("a", MOD_CTRL | MOD_SHIFT))
    _check(failures, "parse_key('Up')", parse_key("Up"), ("Up", 0))

    t = pw.Terminal(20, 4)
    name, mods = parse_key("ctrl-c")
    _check(failures, "key_down('c', CTRL) 的编码", bytes(t.key_down(name, mods)), b"\x03")
    # 反面证据：直接把 "ctrl-c" 丢给库会静默退化成 b'c'（这就是要防的坑）
    naive = bytes(t.key_down("ctrl-c", 0))
    if naive != b"\x03":
        print("  [PASS] 已确认 '必须自己解析' 的前提：key_down('ctrl-c',0)={!r}（≠ b'\\x03'）".format(
            naive))
    else:
        print("  [FAIL] 上游行为变了：key_down('ctrl-c',0) 已能给出 b'\\x03'，"
              "parse_key 的说明该更新")
        failures.append("按键上游行为变化")

    # ── 3. 结果汇总判定矩阵（v1 的两个缺陷固化在这里）
    print("\n── 结果汇总判定 ──")
    cases = [
        ("正常 PASS", "  [PASS] a\n  [PASS] b\n\nSUMMARY: PASS (0 failures)", 0, "PASS"),
        ("自称 PASS 但无 SUMMARY", "  [PASS] a\n", 0, "ERROR"),
        ("零输出 + 退出码 0（v1 会假 PASS）", "", 0, "ERROR"),
        ("无 SUMMARY + 退出码 1", "Traceback...\n", 1, "ERROR"),
        ("正常 FAIL", "  [FAIL] x\n\nSUMMARY: FAIL (2 failures)", 1, "FAIL"),
        # 这一条是护栏：输出里**出现**失败标记字样（但不是行首标记）不得把 PASS 翻成 FAIL。
        # 实测踩过：本用例原本用 "[FAIL]" 当用例名，运行器按"整行包含"判定，
        # 于是自己打了 SUMMARY: PASS 却被判 FAIL（假 FAIL，与 v1 的假 PASS 同类）。
        ("自称 PASS 但输出里提到失败标记字样",
         "  [FAIL] x\n\nSUMMARY: PASS (0 failures)", 0, "FAIL"),
        ("自称 PASS 但退出码 1", "SUMMARY: PASS (1 checks)\n", 1, "ERROR"),
        ("UNSUPPORTED", "  [SKIP] nope\n\nSUMMARY: UNSUPPORTED (pywezterm 不可用)", 0,
         "UNSUPPORTED"),
    ]
    for label, out, code, want in cases:
        s = reporter.Summary.parse("t", out, code)
        _check(failures, "判定 {}".format(label), s.status, want)

    s = reporter.Summary.parse("t", "  [PASS] a\n  [PASS] b\n\nSUMMARY: PASS (0 failures)", 0)
    _check(failures, "checks 兜底（数通过标记行）", s.checks, 2)

    # 行首判定：只有行首的 [FAIL] 才算失败标记（整行包含会把"提到失败标记"误判）
    inline = "  [PASS] 用例名里含 [FAIL] 字样\n\nSUMMARY: PASS (0 failures)"
    _check(failures, "行首判定（提到失败字样不算失败）",
           reporter.Summary.parse("t", inline, 0).status, "PASS")
    at_head = "  [FAIL] 真的失败了\n\nSUMMARY: PASS (0 failures)"
    _check(failures, "行首判定（行首失败标记仍要判 FAIL）",
           reporter.Summary.parse("t", at_head, 0).status, "FAIL")

    rep = reporter.Report()
    for label, out, code, _want in cases:
        rep.add(reporter.Summary.parse(label, out, code))
    tot = rep.totals()
    _check(failures, "FAIL_ITEMS（失败用例数，不重复计）", tot["FAIL_ITEMS"], 6)
    _check(failures, "FAIL_ASSERTS（失败断言数）", tot["FAIL_ASSERTS"], 6)
    _check(failures, "退出码判定用 rep.failed", rep.failed, 6)

    print("\nSUMMARY: {} ({} failures)".format(
        "PASS" if not failures else "FAIL", len(failures)))
    return len(failures)


if __name__ == "__main__":
    sys.exit(run())
