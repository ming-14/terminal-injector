"""SUMMARY 解析与汇总（v2）。

与 v1（tests/e2e/common/reporter.py）的两处刻意差异：

1. **默认不通过**。v1 的 Summary 初始 status 就是 "PASS"，只有输出含 `SUMMARY:`
   行才改写 ⇒ 零输出退出（缺 run()/__main__、导入期就死）的用例被判 PASS。
   v2 反过来：**必须先有 `SUMMARY:` 行**，否则记 ERROR。
2. **失败用例数与失败断言数分开**。v1 的 `total_failures = FAILURES + ERROR + FAIL`，
   而 FAILURES 已含 FAIL 项的断言数，等于把 FAIL 项又加了一遍。

协议（与 v1 一致，便于对照）：
    SUMMARY: PASS (n checks)
    SUMMARY: FAIL (n failures)
    SUMMARY: UNSUPPORTED (<reason>)
"""
import json
import re


VALID = ("PASS", "FAIL", "UNSUPPORTED")


class Summary:
    def __init__(self, name: str, exit_code: int = 0):
        self.name = name
        self.exit_code = exit_code
        self.status = "ERROR"
        self.checks = 0
        self.failures = 0
        self.reason = ""
        self.has_summary = False

    @classmethod
    def parse(cls, name: str, stdout: str, exit_code: int) -> "Summary":
        s = cls(name, exit_code)

        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("SUMMARY:"):
                continue
            rest = line[len("SUMMARY:"):].strip()
            head, _, tail = rest.partition("(")
            status = head.strip().upper()
            detail = tail.rstrip(")").strip() if tail else ""
            s.has_summary = True
            s.status = status if status in VALID else "ERROR"
            if s.status == "UNSUPPORTED":
                s.reason = detail
            m = re.search(r"(\d+)\s+failure", detail)
            if m:
                s.failures = int(m.group(1))
            m = re.search(r"(\d+)\s+check", detail)
            if m:
                s.checks = int(m.group(1))
            elif s.status == "PASS" and detail:
                # 兼容 `SUMMARY: PASS`（无 detail）与别的计数写法
                m = re.search(r"(\d+)", detail)
                if m:
                    s.checks = int(m.group(1))
            break

        # 只认**行首**的 [PASS]/[FAIL] 标记。不能用 `"[FAIL]" in line`：
        # 用例只要把 "[FAIL]" 作为**文本**打印出来（例如断言失败信息里引用它、
        # 或自检用例的用例名里带这两个字符），整行包含判断就会把 PASS 翻成 FAIL
        # —— 实测踩过：脚手架自检用例打了 SUMMARY: PASS，却被判 FAIL（假 FAIL）。
        fail_lines = [l for l in stdout.splitlines() if l.strip().startswith("[FAIL]")]
        pass_lines = [l for l in stdout.splitlines() if l.strip().startswith("[PASS]")]
        if s.checks == 0 and pass_lines:
            # 绝大多数用例只报 failures 不报 checks；用 [PASS] 行数兜底，
            # 否则汇总里"断言总数"恒为 0，看不出用例到底验了什么。
            s.checks = len(pass_lines)

        if not s.has_summary:
            # 没有 SUMMARY 行：绝不当 PASS（v1 的假 PASS 缺陷）
            s.status = "ERROR"
            s.reason = "无 SUMMARY 行 (exit={})".format(exit_code)
            s.failures = max(1, len(fail_lines))
            return s

        if s.status == "PASS":
            if exit_code != 0:
                s.status = "ERROR"
                s.reason = "自称 PASS 但进程退出码 {}".format(exit_code)
            elif fail_lines:
                # 打了 [FAIL] 却报 PASS：以 FAIL 为准（v1 同款兜底）
                s.status = "FAIL"
                s.failures = len(fail_lines)
        if s.status == "FAIL" and s.failures == 0:
            s.failures = max(1, len(fail_lines))

        return s


class Report:
    def __init__(self):
        self.items = []

    def add(self, summary: Summary) -> None:
        self.items.append(summary)

    def totals(self) -> dict:
        counts = {"PASS": 0, "FAIL": 0, "UNSUPPORTED": 0, "ERROR": 0}
        for it in self.items:
            counts[it.status if it.status in counts else "ERROR"] += 1
        counts["TOTAL"] = len(self.items)
        # 失败用例数（FAIL + ERROR）与失败断言数分开，不重复计
        counts["FAIL_ITEMS"] = counts["FAIL"] + counts["ERROR"]
        counts["FAIL_ASSERTS"] = sum(it.failures for it in self.items)
        counts["CHECKS"] = sum(it.checks for it in self.items)
        return counts

    @property
    def failed(self) -> int:
        return self.totals()["FAIL_ITEMS"]

    def print_table(self, duration: float = 0.0) -> None:
        c = self.totals()
        print("\n" + "=" * 72)
        print("e2e_v2 汇总（{} 个用例，耗时 {:.1f}s）".format(c["TOTAL"], duration))
        print("=" * 72)
        for it in self.items:
            line = "  [{:12s}] {}".format(it.status, it.name)
            if it.checks:
                line += "  ({} checks)".format(it.checks)
            if it.status in ("UNSUPPORTED", "ERROR") and it.reason:
                line += "  ({})".format(it.reason)
            if it.status == "FAIL":
                line += "  ({} failures)".format(it.failures)
            print(line)
        print("-" * 72)
        print("  PASS={}  FAIL={}  UNSUPPORTED={}  ERROR={}".format(
            c["PASS"], c["FAIL"], c["UNSUPPORTED"], c["ERROR"]))
        print("  失败用例={}  失败断言={}  断言总数={}".format(
            c["FAIL_ITEMS"], c["FAIL_ASSERTS"], c["CHECKS"]))

    def write_json(self, path: str) -> None:
        data = {
            "totals": self.totals(),
            "items": [{"name": it.name, "status": it.status, "checks": it.checks,
                       "failures": it.failures, "reason": it.reason,
                       "exit_code": it.exit_code} for it in self.items],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
