# -*- coding: utf-8 -*-
"""t_sync.py — 同步输出 (DEC 2026 / synchronized update) 兼容性测试

检验范围:
  · DECRQM CSI ?2026$p 支持判定 (回包 CSI ?2026;n$y)
  · 撕裂对照: 关闭同步 / 开启同步 各重绘约 40 帧, 由用户肉眼判断撕裂与闪白
  · 帧耗时 / 帧间隔统计表, 明显卡顿记 WARN
  · 混色压力: 同步块内一次写入整屏 (约 80x24) 每格不同的随机真彩色, 连续 30 帧

说明:
  · 不支持该模式的终端会忽略 CSI ?2026h/l, 动画仍应正常播放 (此时记 SKIP)。
  · 本文件仅使用标准库, 不修改 termlib.py。
  · TERMTEST_NONINTERACTIVE=1 时跳过所有等待与视觉判定, 仍会跑完动画与统计。
"""

import random
import re
import sys
import time

import termlib as T

SYNC_ON = T.CSI + "?2026h"
SYNC_OFF = T.CSI + "?2026l"
SYNC_QUERY = T.CSI + "?2026$p"

FRAMES = 40          # 撕裂对照每轮帧数
MIX_FRAMES = 30      # 混色压力帧数


def _decrqm(data):
    """解析 DECRQM 回包 CSI ?2026;n$y -> n (None=无回包)。"""
    if not data:
        return None
    m = re.search(rb"\x1b\[\?2026;(\d)\$y", data)
    if m:
        return int(m.group(1))
    m = re.search(rb"2026;(\d)", data)
    return int(m.group(1)) if m else None


def _frame(f, cols, rows):
    """生成一帧整屏图案: 背景色带随帧号上移, 再加一个水平移动的高亮标记。"""
    parts = []
    for y in range(rows):
        parts.append(T.cup(y + 1, 1))
        band = (y + f) % 6
        parts.append(T.bg_rgb(24, 48, 96) if band < 3 else T.bg_rgb(96, 48, 24))
        parts.append(" " * cols)
    parts.append(T.sgr(0))
    x = (f * 4) % max(1, cols - 24)
    parts.append(T.cup(rows // 2 + 1, x + 1))
    parts.append(T.sgr(0, 97, 41) + "  >>> termtest 同步输出 DEC2026 <<<  " + T.sgr(0))
    return "".join(parts)


def _sec_support():
    T.section("DECRQM · 同步输出支持判定")
    T.hint("查询 CSI ?2026$p, 回包 CSI ?2026;n$y (0=未识别 1=set 2=reset 3=永久set 4=永久reset)。")
    r = T.query(SYNC_QUERY)
    n = _decrqm(r)
    if n is None:
        T.row("DECRQM ?2026 支持", T.SKIP, "无有效回包, 视为不实现该序列")
    elif n == 0:
        T.row("DECRQM ?2026 支持", T.SKIP, "回包 n=0: 终端不识别 DEC 2026 (CSI ?2026;0$y)")
    else:
        # 回包原文不落盘: 查询窗口里可能混着用户键入, 只回传解析后的状态值
        T.row("DECRQM ?2026 支持", T.PASS, "回包 CSI ?2026;%d$y (%d 字节)" % (n, len(r)))


def _run_round(sync, cols, rows):
    """跑一轮动画, 返回每帧统计 (序号, 字节数, 写耗时秒, 与上一帧起始间隔秒)。"""
    stats = []
    prev_start = None
    try:
        if sync:
            T.write(SYNC_ON)
        for f in range(FRAMES):
            start = time.perf_counter()
            data = _frame(f, cols, rows).encode("utf-8")
            T.write(data)
            dt = time.perf_counter() - start
            gap = 0.0 if prev_start is None else (start - prev_start)
            prev_start = start
            stats.append((f + 1, len(data), dt, gap))
            if not T.NONINTERACTIVE:
                time.sleep(0.016)
    finally:
        if sync:
            # 异常/中断也不例外: 同步输出一旦留在"开着"的状态, 后面的输出就都不刷新了
            T.write(SYNC_OFF)
    return stats


def _print_stats(stats, title):
    """打印帧间隔表, 返回最大帧间隔 (秒)。"""
    T.hint(title)
    worst = 0.0
    total = 0
    for idx, nb, dt, gap in stats:
        total += nb
        if gap > worst:
            worst = gap
        if not T.NONINTERACTIVE or idx <= 3:
            T.wln("    #%02d  %6dB  写 %6.2fms  间隔 %6.2fms" % (
                idx, nb, dt * 1000.0, gap * 1000.0))
    if T.NONINTERACTIVE and len(stats) > 3:
        T.hint("    (非交互模式: 其余 %d 帧明细省略)" % (len(stats) - 3))
    T.hint("本轮共 %d 帧 / %d 字节, 最大帧间隔 %.2fms" % (len(stats), total, worst * 1000.0))
    return worst


def _sec_tearing(cols, rows):
    T.section("撕裂对照 · 关闭 / 开启同步输出")
    T.note("请盯住屏幕: 关闭同步时整屏重绘可能出现水平撕裂或瞬间闪白。")

    # ---- 第一轮: 关闭同步
    T.mode_reset()               # 先把所有模式清空 (含 ?2026l), 从确定状态开始对照
    T.hint("已发送 CSI ?2026l (关闭同步输出), 开始第一轮动画 ...")
    s1 = _run_round(False, cols, rows)
    T.wln("")
    # 动画把整屏都画满了, 光标停在画面中间; 提问与判定要落到图案之外的行上,
    # 否则会接在图案里、把画面糊掉。
    T.park_cursor(max(1, rows - 2))
    a1 = T.ask_yn("关闭同步输出时, 是否看到水平撕裂 / 闪白?")
    if a1 is True:
        T.row("关闭同步 · 撕裂观察", T.PASS, "观察到撕裂 (符合预期)")
    elif a1 is False:
        T.row("关闭同步 · 撕裂观察", T.DIFF, "未观察到撕裂 (重绘可能足够快)")
    else:
        T.row("关闭同步 · 撕裂观察", T.SKIP, "未作答 / 非交互模式")
    w1 = _print_stats(s1, "第一轮 (同步关闭) 帧统计:")
    T.row("帧间隔 (同步关闭)", T.WARN if w1 > 0.1 else T.PASS, "最大间隔 %.2fms" % (w1 * 1000.0))

    # ---- 第二轮: 开启同步
    T.wln("")
    T.write(SYNC_ON)
    T.hint("已发送 CSI ?2026h (开启同步输出), 开始第二轮动画 ...")
    s2 = _run_round(True, cols, rows)
    T.write(SYNC_OFF)
    T.wln("")
    T.park_cursor(max(1, rows - 2))
    a2 = T.ask_yn("开启同步输出时, 画面是否完整、无撕裂?")
    if a2 is True:
        T.row("开启同步 · 无撕裂", T.PASS, "画面完整 (符合预期)")
    elif a2 is False:
        T.row("开启同步 · 无撕裂", T.FAIL, "仍观察到撕裂 / 闪白")
    else:
        T.row("开启同步 · 无撕裂", T.SKIP, "未作答 / 非交互模式")
    w2 = _print_stats(s2, "第二轮 (同步开启) 帧统计:")
    T.row("帧间隔 (同步开启)", T.WARN if w2 > 0.1 else T.PASS, "最大间隔 %.2fms" % (w2 * 1000.0))
    T.write(SYNC_OFF)


def _sec_mixing(cols, rows):
    T.section("混色压力 · 同步块内整屏随机真彩色")
    ncols = min(cols, 80)
    nrows = min(rows, 24)
    T.hint("在 CSI ?2026h ... l 之间一次写入 %dx%d 每格不同的随机真彩色, 连续 %d 帧。"
           % (ncols, nrows, MIX_FRAMES))
    rnd = random.Random(2026)
    T.mode_reset()               # 从确定状态开始 (含 ?2026l)
    try:
        for _f in range(MIX_FRAMES):
            buf = [SYNC_ON]
            for y in range(1, nrows + 1):
                buf.append(T.cup(y, 1))
                for _x in range(ncols):
                    buf.append(T.bg_rgb(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
                    buf.append(" ")
            buf.append(T.sgr(0))
            buf.append(SYNC_OFF)
            T.write("".join(buf))
            if not T.NONINTERACTIVE:
                time.sleep(0.03)
    finally:
        T.write(SYNC_OFF)        # 异常/中断也不能留在同步块里
    # 图案只占 1..nrows 行, 提问与判定放到图案下方, 别压在刚看的画面上
    T.park_cursor(max(1, min(rows - 2, nrows + 2)))
    a = T.ask_yn("压力测试中是否出现半屏更新 / 撕裂?")
    if a is True:
        T.row("混色压力 · 整屏随机真彩色", T.FAIL, "出现半屏更新 / 撕裂")
    elif a is False:
        T.row("混色压力 · 整屏随机真彩色", T.PASS, "未出现半屏更新")
    else:
        T.row("混色压力 · 整屏随机真彩色", T.SKIP, "未作答 / 非交互模式")
    T.write(SYNC_OFF)


def main():
    global FRAMES, MIX_FRAMES
    if T.NONINTERACTIVE:          # 冒烟运行时大幅缩短, 避免刷屏
        FRAMES = 5
        MIX_FRAMES = 3
    T.start("t_sync.py",
            ["DECRQM ?2026 支持", "关闭同步撕裂对照", "开启同步撕裂对照",
             "帧耗时/间隔统计", "整屏混色压力"],
            note="检验同步输出 DEC 2026: 撕裂对照靠肉眼观察, 帧统计自动判定")
    cols, rows = T.term_size()
    cols = max(20, cols)
    rows = max(6, rows)
    _sec_support()
    _sec_tearing(cols, rows)
    _sec_mixing(cols, rows)
    T.finish()


if __name__ == "__main__":
    sys.exit(T.guard(main))