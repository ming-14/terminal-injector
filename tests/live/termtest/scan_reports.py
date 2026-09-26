# -*- coding: utf-8 -*-
"""scan_reports.py — 报告红线扫描 (发布前自检)

用与 termlib.redact 同一套正则, 扫描 _reports/*.txt 里"不应出现在发布面"的内容:
用户路径 / 邮箱 (RFC 2606 保留域除外) / IPv4 / WT_SESSION GUID / 私钥标记 /
file:// 主机。命中即退出码 1, 可直接挂 CI 或打包前手动运行。

用法:
    python scan_reports.py            # 扫 _reports/
    python scan_reports.py 某目录     # 扫指定目录
"""

import os
import re
import sys

import termlib as T

PATTERNS = [
    ("用户路径 (含用户名)", re.compile(r"[A-Za-z]:[\\/]Users[\\/][^\\/\"'<>\s]+")),
    ("邮箱 (example.* 除外)", re.compile(r"\b[\w.]+@(?!example\.(com|org|net)\b)[\w.]+\.\w{2,}\b")),
    ("IPv4 地址", re.compile(r"\b\d{1,3}(\.\d{1,3}){3}\b")),
    ("WT_SESSION 会话 GUID", re.compile(r"WT_SESSION=[0-9a-fA-F-]{36}")),
    ("私钥块标记", re.compile(r"-----BEGIN[ A-Z]*PRIVATE KEY-----")),
    ("file:// 主机名", re.compile(r"file://(?!termtest-host)[^/\s]+/")),
]

# 命中后展示的上下文宽度 (截断展示, 不把命中内容整行带出来)
SNIP = 60


def scan_file(path):
    hits = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                for name, pat in PATTERNS:
                    m = pat.search(line)
                    if m:
                        col = m.start()
                        lo, hi = max(0, col - 15), min(len(line.rstrip("\n")), col + SNIP)
                        hits.append((lineno, name, line[lo:hi].rstrip()))
    except OSError as e:
        hits.append((0, "读取失败", repr(e)))
    return hits


def main():
    d = sys.argv[1] if len(sys.argv) > 1 else os.path.join(T.HERE, "_reports")
    if not os.path.isdir(d):
        print("目录不存在: %s" % d)
        return 2
    total = 0
    files = sorted(fn for fn in os.listdir(d) if fn.lower().endswith(".txt"))
    for fn in files:
        hits = scan_file(os.path.join(d, fn))
        for lineno, name, snip in hits:
            total += 1
            print("HIT  %s:%s  [%s]  …%s…" % (fn, lineno, name, snip))
    print("----")
    print("扫描 %d 个文件, 命中 %d 处 (正则集与 termlib.redact 一致)" % (len(files), total))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
