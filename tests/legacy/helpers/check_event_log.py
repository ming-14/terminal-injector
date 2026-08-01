"""查询 Windows 事件日志中 cmd.exe 的崩溃记录。

用 pywin32 的 win32evtlog 读取 Application 日志，
找最近 1 小时内 EventID 1000 (Application Error) 和 1001 (WER) 的记录。
"""
import os
import sys
import time
from datetime import datetime, timedelta

import win32evtlog
import win32con
import win32evtlogutil


def query_app_log(hours: int = 1):
    """查询 Application 日志中最近 hours 小时内的 Error/Warning 事件。"""
    server = "localhost"
    logtype = "Application"
    hand = win32evtlog.OpenEventLog(server, logtype)

    # 读取方向：EVENTLOG_BACKWARDS_READ | EVENTLOG_SEQUENTIAL_READ
    flags = win32evtlog.EVENTLOG_BACKWARDS_READ | win32evtlog.EVENTLOG_SEQUENTIAL_READ
    events = win32evtlog.ReadEventLog(hand, flags, 0)

    cutoff = datetime.now() - timedelta(hours=hours)
    print("[query] cutoff={} events_count={}".format(cutoff, len(events)))

    found = 0
    for evt in events:
        # evt.TimeGenerated 是 pywintypes.Time
        try:
            tg = evt.TimeGenerated.timestamp()
        except Exception:
            continue
        if tg < cutoff.timestamp():
            continue

        # 关注 EventID 1000 (Application Error) 和 1001 (WER Report)
        eid = evt.EventID & 0xFFFF
        if eid not in (1000, 1001, 1026):
            continue

        # 转换 SourceName
        src = str(evt.SourceName)
        # 只关注 cmd.exe 相关
        msg = ""
        try:
            msg = win32evtlogutil.SafeFormatMessage(evt, logtype)
        except Exception as e:
            msg = "<format failed: {}>".format(e)

        if "cmd.exe" not in msg and "cmd" not in src.lower():
            continue

        print("\n=== Event ===")
        print("  TimeGenerated: {}".format(evt.TimeGenerated))
        print("  Source: {}".format(src))
        print("  EventID: {} (0x{:X})".format(eid, evt.EventID))
        print("  EventType: {}".format(evt.EventType))
        print("  Message: {}".format(msg[:500]))
        found += 1
        if found >= 10:
            break

    win32evtlog.CloseEventLog(hand)
    print("\n[done] found {} events".format(found))


if __name__ == "__main__":
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    query_app_log(hours)
