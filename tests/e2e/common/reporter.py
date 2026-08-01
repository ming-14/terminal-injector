"""统一报告模块。

每个测试文件 run() 结束打印一行 SUMMARY，run_all 抓取汇总：
  SUMMARY: PASS (n checks)         或
  SUMMARY: FAIL (n failures)       或
  SUMMARY: UNSUPPORTED (<reason>)
"""
import json
import os


class Summary:
    """单个测试文件的执行结果。"""

    def __init__(self, name: str, exit_code: int = 0):
        self.name = name
        self.exit_code = exit_code
        self.status = "PASS" if exit_code == 0 else "FAIL"
        self.failures = 0 if exit_code == 0 else exit_code
        self.checks = 0
        self.reason = ""

    @classmethod
    def parse(cls, name: str, stdout: str, exit_code: int) -> "Summary":
        """从子进程输出解析 SUMMARY 行与 FAIL 行。"""
        s = cls(name, exit_code)
        fail_lines = [l for l in stdout.splitlines() if "[FAIL]" in l]
        for line in stdout.splitlines():
            line = line.strip()
            if line.startswith("SUMMARY:"):
                rest = line[len("SUMMARY:"):].strip()
                parts = rest.split("(", 1)
                s.status = parts[0].strip().upper()
                if len(parts) > 1:
                    detail = parts[1].rstrip(")")
                    if s.status == "FAIL":
                        import re
                        m = re.search(r"(\d+) failure", detail)
                        if m:
                            s.failures = int(m.group(1))
                    elif s.status == "UNSUPPORTED":
                        s.reason = detail
                break
        if s.status == "PASS" and fail_lines:
            s.failures = len(fail_lines)
            s.status = "FAIL" if s.failures else s.status
        s.checks = max(s.checks, 0)
        return s


class Report:
    """整个测试运行期的汇总。"""

    def __init__(self):
        self.items = []  # list[Summary]
        self.started = 0.0

    def add(self, summary: Summary) -> None:
        self.items.append(summary)

    def totals(self) -> dict:
        counts = {"PASS": 0, "FAIL": 0, "UNSUPPORTED": 0, "ERROR": 0}
        for it in self.items:
            status = it.status if it.status in counts else "ERROR"
            counts[status] += 1
        counts["TOTAL"] = len(self.items)
        counts["FAILURES"] = sum(it.failures for it in self.items if it.status == "FAIL")
        return counts

    def print_table(self) -> None:
        counts = self.totals()
        print("\n" + "=" * 64)
        print("汇总报告（{} 个测试）".format(counts["TOTAL"]))
        print("=" * 64)
        for it in self.items:
            mark = {"PASS": "PASS", "FAIL": "FAIL", "UNSUPPORTED": "UNSUPPORTED",
                    "ERROR": "ERROR"}.get(it.status, it.status)
            line = "  [{:12s}] {}".format(mark, it.name)
            if it.status == "UNSUPPORTED" and it.reason:
                line += "  ({})".format(it.reason)
            if it.status == "FAIL":
                line += "  ({} failures)".format(it.failures)
            print(line)
        print("-" * 64)
        print("  PASS={}  FAIL={}  UNSUPPORTED={}  ERROR={}".format(
            counts["PASS"], counts["FAIL"], counts["UNSUPPORTED"], counts["ERROR"]))
        if counts["FAILURES"]:
            print("  失败断言总数: {}".format(counts["FAILURES"]))

    def write_json(self, path: str) -> None:
        counts = self.totals()
        data = {
            "totals": counts,
            "items": [{"name": it.name, "status": it.status,
                       "failures": it.failures, "reason": it.reason}
                      for it in self.items],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @property
    def total_failures(self) -> int:
        return self.totals()["FAILURES"] + self.totals()["ERROR"] + self.totals()["FAIL"]
