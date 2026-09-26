# -*- coding: utf-8 -*-
"""t_perf.py — 性能与压力测试 (尽量全自动, 不需要人工判断)

检验范围:
  * 往返延迟 RTT: CSI 6n (DSR 光标位置报告) 往返, 反映 按键→终端→程序 链路开销
  * 动画帧率: 全屏移动方块 60fps 目标, 分别用 全量重绘 / 增量更新 (逐格 CUP diff)
  * 输出吞吐: 大量带颜色/属性文本的行每秒 / MB 每秒, 每 5000 行用 CUP 刷新进度
  * 超长行: 单行 100 万字符, 检验自动换行 (软换行) 路径的耗时
  * 属性风暴: 80x24 全屏随机 truecolor 背景连续刷帧, 观察是否花屏/撕裂
  * 滚动: CSI n S / CSI n T 大范围滚动, 以及纯 LF 洪水滚动
  * 颜色切换压力: 每个格子都切换 SGR 的全屏帧开销

说明:
  * 判定阈值是经验值, 受终端实现、窗口大小、是否走 GPU 影响很大, 异常项一律记 WARN
    并给出可能原因 (而不是 FAIL), 便于横向对比不同终端。
  * 所有计时统一用 time.perf_counter(): Windows 上 time.monotonic() 走 GetTickCount64,
    分辨率只有 ~15.6ms, 会把单帧耗时量化成 0 或 15.6ms, 完全无法用于性能判定。
  * 视觉类测试跑在备用屏上, 避免污染滚动缓冲; 吞吐/滚动类测试跑在主屏, 以覆盖
    滚动缓冲 (scrollback) 路径。
  * 非交互模式 (TERMTEST_NONINTERACTIVE=1) 下不等待任何输入; 冒烟时可缩小参数:
    python t_perf.py --lines 2000 --seconds 1 --frames 20
"""

import argparse
import random
import re
import sys
import time

import termlib as T

BLOCK_CH = "#"
ST_BLOCK = T.sgr(1, 92)

# 经验阈值 (毫秒 / 比率)
TH_RTT_OK = 20.0            # 单次往返延迟
TH_RTT_WARN = 50.0
TH_FRAME_60FPS = 16.7       # 单帧渲染耗时 60fps 预算
TH_FRAME_30FPS = 33.3
TH_LPS_OK = 30000.0         # 行/秒
TH_LPS_WARN = 10000.0
TH_MBPS_OK = 20.0           # 长行渲染 MB/秒
TH_STORM_FPS_OK = 60.0      # 属性风暴帧率
TH_STORM_FPS_WARN = 20.0
TH_SCROLL_OPS_OK = 2000.0   # 滚动操作/秒
TH_LF_OK = 100000.0         # LF 洪水 行/秒


# ------------------------------------------------------------------ 工具
def _cursor_pos(timeout=0.5):
    """查询光标位置, 返回 (row, col) 或 None。"""
    r = T.query(T.CSI + "6n", timeout=timeout, quiet=0.01)
    if not r:
        return None
    m = re.search(rb"\x1b\[(\d+);(\d+)R", r)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _runs(sorted_cols):
    """把有序列集合压成连续区间 [[a,b], ...]。"""
    runs = []
    for c in sorted_cols:
        if runs and c == runs[-1][1] + 1:
            runs[-1][1] = c
        else:
            runs.append([c, c])
    return runs


def _fmt(k, v, unit=""):
    T.wln("  " + T.sgr(90) + T.pad(k, 22) + T.sgr(0) + str(v) + unit)


# ------------------------------------------------------------------ 1. RTT
def bench_rtt(times=20):
    t = time.perf_counter()
    probe = T.query(T.CSI + "6n", timeout=0.4, quiet=0.02)
    probe_ms = (time.perf_counter() - t) * 1000.0
    if not probe:
        return {"ok": False, "probe_ms": probe_ms}
    res = {"ok": True, "probe_ms": probe_ms,
           "resp": "%d 字节 (回包不落盘, 只记长度)" % len(probe), "samples": []}
    for _ in range(times):
        t0 = time.perf_counter()
        r = T.query(T.CSI + "6n", timeout=0.5, quiet=0.01)
        if r:
            res["samples"].append((time.perf_counter() - t0) * 1000.0)
    s = res["samples"]
    if not s:
        res["ok"] = False
        return res
    res.update(n=len(s), min=min(s), avg=sum(s) / len(s), max=max(s))
    return res


# ------------------------------------------------------------------ 2. FPS
def _move_rect(bx, by, dx, dy, w, h, bw, bh):
    nbx, nby = bx + dx, by + dy
    if nbx < 0:
        nbx, dx = 0, -dx
    elif nbx + bw > w:
        nbx, dx = w - bw, -dx
    if nby < 0:
        nby, dy = 0, -dy
    elif nby + bh > h:
        nby, dy = h - bh, -dy
    return nbx, nby, dx, dy


def _frame_full(w, h, bx, by, bw, bh):
    """全量重绘: 整个动画区域每帧全部重写。"""
    out = []
    for y in range(h):
        out.append(T.cup(y + 1, 1))
        if by <= y < by + bh:
            out.append(" " * bx)
            out.append(ST_BLOCK + BLOCK_CH * bw + T.sgr(0))
            out.append(" " * (w - bx - bw))
        else:
            out.append(" " * w)
    return "".join(out)


def _frame_incr(old, new, w, bw, bh):
    """增量更新: 只重写旧/新方块覆盖范围里真正变化的格子。"""
    ox, oy = old
    nx, ny = new
    out = []
    for y in range(min(oy, ny), max(oy + bh, ny + bh)):
        o = set(range(ox, ox + bw)) if oy <= y < oy + bh else set()
        n = set(range(nx, nx + bw)) if ny <= y < ny + bh else set()
        for a, b in _runs(sorted(o - n)):
            out.append(T.cup(y + 1, a + 1) + " " * (b - a + 1))
        for a, b in _runs(sorted(n - o)):
            out.append(T.cup(y + 1, a + 1) + ST_BLOCK + BLOCK_CH * (b - a + 1) + T.sgr(0))
    return "".join(out)


def bench_fps(seconds, incremental):
    """按 60fps 目标节奏跑动画, 返回实际帧率与单帧渲染耗时 (ms)。

    用绝对时间点排程 (t_start + i*period) 而不是相对 sleep, 这样即使系统 sleep
    粒度偏粗, 也能自动校正、不累积漂移。单帧耗时只统计 生成+写入, 不含等待。
    """
    cols, rows = T.term_size()
    w = max(20, min(cols, 200))
    h = max(6, rows - 1)
    bw = max(1, min(8, w // 4))
    bh = max(1, min(3, h // 3))
    bx, by, dx, dy = 0, 0, 1, 1
    period = 1.0 / 60.0
    frames, total, mx, nb = 0, 0.0, 0.0, 0
    T.alt_on()
    t_start = time.perf_counter()
    t_end = t_start + seconds
    try:
        while True:
            target = t_start + frames * period
            slack = target - time.perf_counter()
            if slack > 0:
                time.sleep(slack)
            if time.perf_counter() >= t_end:
                break
            nbx, nby, dx, dy = _move_rect(bx, by, dx, dy, w, h, bw, bh)
            t0 = time.perf_counter()
            s = _frame_incr((bx, by), (nbx, nby), w, bw, bh) if incremental \
                else _frame_full(w, h, nbx, nby, bw, bh)
            T.write(s)
            dt = (time.perf_counter() - t0) * 1000.0
            bx, by = nbx, nby
            frames += 1
            total += dt
            mx = max(mx, dt)
            nb += len(s)
    finally:
        T.alt_off()
    elapsed = max(1e-6, time.perf_counter() - t_start)
    return {"area": (w, h), "block": (bw, bh), "frames": frames, "sec": elapsed,
            "avg_ms": total / max(1, frames), "max_ms": mx, "bytes": nb,
            "fps": frames / elapsed, "mbps": nb / 1e6 / elapsed}


# ------------------------------------------------------------------ 3. 吞吐
def bench_throughput(nlines, batch=200, progress_every=5000):
    cols, rows = T.term_size()
    styles = [T.sgr(0), T.sgr(1), T.sgr(31), T.sgr(1, 32), T.sgr(33),
              T.sgr(34), T.bg(236) + T.fg(15), T.sgr(4, 35)]
    text = "0123456789 abcdefghijklmnopqrstuvwxyz ABCDEFGHIJKLMNOPQRST The quick brown fox"
    T.clear()
    buf, nb = [], 0
    t0 = time.perf_counter()
    for i in range(nlines):
        buf.append("%s%08d  %s%s\r\n" % (styles[i % len(styles)], i, text, T.sgr(0)))
        if len(buf) >= batch:
            s = "".join(buf)
            T.write(s)
            nb += len(s)
            buf = []
        if (i + 1) % progress_every == 0:
            if buf:
                s = "".join(buf)
                T.write(s)
                nb += len(s)
                buf = []
            T.write(T.cup(rows, 1) + T.sgr(0, 46) + T.sgr(30)
                    + " 进度 %d/%d (%.0f%%) " % (i + 1, nlines, (i + 1) * 100.0 / nlines)
                    + T.sgr(0))
    if buf:
        s = "".join(buf)
        T.write(s)
        nb += len(s)
    dt = max(1e-6, time.perf_counter() - t0)
    return {"lines": nlines, "sec": dt, "bytes": nb, "lps": nlines / dt,
            "mbps": nb / 1e6 / dt}


# ------------------------------------------------------------------ 4. 超长行
def bench_longline(nchars):
    pat = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = (pat * (nchars // len(pat) + 1))[:nchars]
    T.clear()
    t0 = time.perf_counter()
    T.write(s + "\r\n")
    dt = max(1e-6, time.perf_counter() - t0)
    return {"chars": nchars, "sec": dt, "ms": dt * 1000.0,
            "mbps": nchars / 1e6 / dt, "wrapped": 1.0 * nchars / max(1, T.term_size()[0])}


# ------------------------------------------------------------------ 5. 属性风暴
def bench_attr_storm(frames, cell_w=80, cell_h=24):
    cols, rows = T.term_size()
    w, h = min(cols, cell_w), min(rows, cell_h)
    rng = random.Random(7)
    build_ms, write_ms, mx_write = 0.0, 0.0, 0.0
    nb = 0
    T.alt_on()
    t_start = time.perf_counter()
    try:
        for _ in range(frames):
            tb = time.perf_counter()
            out = []
            for y in range(h):
                out.append(T.cup(y + 1, 1))
                for _x in range(w):
                    out.append(T.bg_rgb(rng.randrange(256), rng.randrange(256),
                                        rng.randrange(256)))
                    out.append(" ")
            out.append(T.sgr(0))
            s = "".join(out)
            tb = (time.perf_counter() - tb) * 1000.0
            tw = time.perf_counter()
            T.write(s)
            tw = (time.perf_counter() - tw) * 1000.0
            build_ms += tb
            write_ms += tw
            mx_write = max(mx_write, tw)
            nb += len(s)
    finally:
        T.alt_off()
    elapsed = max(1e-6, time.perf_counter() - t_start)
    n = max(1, frames)
    return {"area": (w, h), "frames": frames, "sec": elapsed, "fps": frames / elapsed,
            "build_ms": build_ms / n, "write_ms": write_ms / n, "max_write_ms": mx_write,
            "frame_ms": (build_ms + write_ms) / n, "bytes": nb, "mbps": nb / 1e6 / elapsed}


# ------------------------------------------------------------------ 6. 滚动
def bench_scroll(iters, speed=1.0, lf_lines=50000):
    cols, rows = T.term_size()
    n = max(1, int(3 * speed))
    T.clear()
    T.write(T.cup(rows, 1))
    t0 = time.perf_counter()
    for i in range(iters):
        T.write(T.CSI + ("%dS" % n if i % 2 == 0 else "%dT" % n))
    dt_csi = max(1e-6, time.perf_counter() - t0)

    t0 = time.perf_counter()
    T.write("\n" * lf_lines)
    dt_lf = max(1e-6, time.perf_counter() - t0)
    return {"iters": iters, "step": n, "sec": dt_csi, "ops": iters / dt_csi,
            "lf_lines": lf_lines, "lf_sec": dt_lf, "lf_lps": lf_lines / dt_lf}


# ------------------------------------------------------------------ 7. 颜色切换
def bench_color_switch(frames=5, cell_w=80, cell_h=24):
    cols, rows = T.term_size()
    w, h = min(cols, cell_w), min(rows, cell_h)
    total = 0.0
    nb = 0
    T.alt_on()
    try:
        for f in range(frames):
            out = []
            for y in range(h):
                out.append(T.cup(y + 1, 1))
                for x in range(w):
                    out.append(T.fg((x * 7 + y * 3 + f * 11) % 256))
                    out.append(T.bg((x * 13 + y * 5 + f * 17) % 256))
                    out.append(" ")
            out.append(T.sgr(0))
            s = "".join(out)
            t0 = time.perf_counter()
            T.write(s)
            total += (time.perf_counter() - t0) * 1000.0
            nb += len(s)
    finally:
        T.alt_off()
    n = max(1, frames)
    return {"area": (w, h), "frames": frames, "cell_ms": total / n,
            "cells": w * h, "bytes": nb, "per_cell_us": total * 1000.0 / n / max(1, w * h)}


# ------------------------------------------------------------------ 汇总
def _table(title, items):
    T.divider(title)
    for k, v in items:
        T.wln("  " + T.sgr(90) + T.pad(k, 24) + T.sgr(0) + str(v))


def main():
    ap = argparse.ArgumentParser(description="termtest 性能与压力测试 (全自动)")
    ap.add_argument("--lines", type=int, default=100000, help="吞吐测试行数 (默认 100000)")
    ap.add_argument("--seconds", type=float, default=3.0, help="动画测试时长, 秒 (默认 3)")
    ap.add_argument("--frames", type=int, default=200, help="属性风暴帧数 (默认 200)")
    ap.add_argument("--speed", type=float, default=1.0, help="滚动/洪水压力倍数 (默认 1)")
    args = ap.parse_args()

    T.start("t_perf.py",
            ["RTT 往返延迟", "全量/增量 60fps 动画", "输出吞吐 lines/s 与 MB/s",
             "100 万字符超长行", "truecolor 属性风暴", "CSI S/T 与 LF 滚动",
             "逐格 SGR 颜色切换"],
            note="全自动性能压测; 异常项记 WARN 并给出可能原因, 具体数字见报告文件。")
    T.hint("参数: --lines %d  --seconds %.1f  --frames %d  --speed %.2f"
           % (args.lines, args.seconds, args.frames, args.speed))

    results = {}
    rep = ["t_perf.py 性能与压力报告 (窗口 %dx%d)" % T.term_size(), ""]

    # 1. RTT
    T.section("1. 往返延迟 RTT — CSI 6n 光标位置报告")
    rtt = bench_rtt(20)
    results["rtt"] = rtt
    T.hint("数值含 termlib.query() 的读取收敛开销 (约 10~30ms), 适合横向对比而不宜当绝对值")
    if rtt.get("ok"):
        _fmt("首次探测响应", "%.1f ms" % rtt["probe_ms"])
        _fmt("抽样次数", rtt["n"])
        _fmt("min / avg / max", "%.1f / %.1f / %.1f ms" % (rtt["min"], rtt["avg"], rtt["max"]))
        _fmt("回包样例", rtt["resp"])
        rep += ["# RTT (CSI 6n)", "首次探测=%.2fms" % rtt["probe_ms"],
                "n=%d min=%.2f avg=%.2f max=%.2f (ms)" % (rtt["n"], rtt["min"], rtt["avg"], rtt["max"]),
                "回包=%s" % rtt["resp"], ""]
    else:
        T.wln("  " + T.sgr(33) + "无回包: 终端可能未实现 DSR(6n) 查询" + T.sgr(0))
        rep += ["# RTT (CSI 6n): 无回包 (终端未实现该查询)", ""]

    # 2. FPS 全量
    T.section("2. 动画帧率 — 全屏移动方块 (全量重绘, 目标 60fps)")
    fps_full = bench_fps(max(0.5, args.seconds), incremental=False)
    results["fps_full"] = fps_full
    _fmt("动画区域/方块", "%dx%d / %dx%d" % (fps_full["area"][0], fps_full["area"][1],
                                          fps_full["block"][0], fps_full["block"][1]))
    _fmt("帧数 / 实际帧率", "%d 帧 / %.1f fps" % (fps_full["frames"], fps_full["fps"]))
    _fmt("单帧耗时 avg/max", "%.2f / %.2f ms" % (fps_full["avg_ms"], fps_full["max_ms"]))
    _fmt("渲染吞吐", "%.2f MB/s" % fps_full["mbps"])
    rep += ["# FPS 全量重绘", "区域=%dx%d 方块=%dx%d 帧数=%d 实际=%.2ffps avg=%.3fms max=%.3fms %.2fMB/s"
            % (fps_full["area"][0], fps_full["area"][1], fps_full["block"][0],
               fps_full["block"][1], fps_full["frames"], fps_full["fps"],
               fps_full["avg_ms"], fps_full["max_ms"], fps_full["mbps"]), ""]

    T.section("3. 动画帧率 — 同一动画的增量更新 (逐格 CUP diff)")
    fps_incr = bench_fps(max(0.5, args.seconds), incremental=True)
    results["fps_incr"] = fps_incr
    _fmt("帧数 / 实际帧率", "%d 帧 / %.1f fps" % (fps_incr["frames"], fps_incr["fps"]))
    _fmt("单帧耗时 avg/max", "%.2f / %.2f ms" % (fps_incr["avg_ms"], fps_incr["max_ms"]))
    _fmt("渲染吞吐", "%.2f MB/s" % fps_incr["mbps"])
    ratio = fps_full["avg_ms"] / max(1e-6, fps_incr["avg_ms"])
    _fmt("全量 / 增量 单帧耗时", "%.2fx (增量更省时则 > 1)" % ratio)
    rep += ["# FPS 增量更新", "帧数=%d 实际=%.2ffps avg=%.3fms max=%.3fms %.2fMB/s"
            % (fps_incr["frames"], fps_incr["fps"], fps_incr["avg_ms"],
               fps_incr["max_ms"], fps_incr["mbps"]),
            "全量/增量 单帧耗时比=%.2f" % ratio, ""]

    # 4. 属性风暴
    T.section("4. 属性风暴 — 全屏随机 truecolor 背景连续刷帧")
    storm = bench_attr_storm(args.frames)
    results["storm"] = storm
    _fmt("区域 / 帧数", "%dx%d / %d" % (storm["area"][0], storm["area"][1], storm["frames"]))
    _fmt("实测 fps", "%.1f fps" % storm["fps"])
    _fmt("每帧 生成/写入", "%.2f / %.2f ms" % (storm["build_ms"], storm["write_ms"]))
    _fmt("最慢一帧写入", "%.2f ms" % storm["max_write_ms"])
    _fmt("输出量", "%.2f MB (%.2f MB/s)" % (storm["bytes"] / 1e6, storm["mbps"]))
    rep += ["# 属性风暴", "区域=%dx%d 帧数=%d fps=%.2f 每帧生成=%.3fms 写入=%.3fms 最慢=%.3fms %.2fMB/s"
            % (storm["area"][0], storm["area"][1], storm["frames"], storm["fps"],
               storm["build_ms"], storm["write_ms"], storm["max_write_ms"], storm["mbps"]), ""]

    # 风暴之后立刻校验一次光标定位是否仍准确 (自动化的"花屏/状态错乱"探针)
    T.write(T.cup(1, 1) + T.sgr(0))
    pos = _cursor_pos()
    _fmt("风暴后 CSI 6n 定位", ("%s (期望 (1, 1))" % (pos,)) if pos else "无回包")

    # 5. 颜色切换压力
    T.section("5. 颜色切换压力 — 每格都换 SGR 的全屏帧")
    csw = bench_color_switch(5)
    results["csw"] = csw
    _fmt("区域 / 格子数", "%dx%d / %d" % (csw["area"][0], csw["area"][1], csw["cells"]))
    _fmt("单帧耗时", "%.2f ms" % csw["cell_ms"])
    _fmt("单格平均", "%.1f µs/格" % csw["per_cell_us"])
    rep += ["# 颜色切换压力", "区域=%dx%d 单元格=%d 单帧=%.3fms 单格=%.2fus 输出=%.2fMB"
            % (csw["area"][0], csw["area"][1], csw["cells"], csw["cell_ms"],
               csw["per_cell_us"], csw["bytes"] / 1e6), ""]

    # 6. 滚动
    T.section("6. 滚动压力 — CSI n S / CSI n T 与纯 LF 洪水 (会写入滚动缓冲)")
    scr = bench_scroll(int(5000 * args.speed), args.speed)
    results["scroll"] = scr
    _fmt("CSI S/T 次数", "%d 次 (每次 %d 行)" % (scr["iters"], scr["step"]))
    _fmt("CSI S/T 速度", "%.1f 次/秒" % scr["ops"])
    _fmt("LF 洪水", "%d 行 / %.0f 行/秒" % (scr["lf_lines"], scr["lf_lps"]))
    rep += ["# 滚动", "CSI S/T %d 次(每次 %d 行) %.1f 次/秒; LF %d 行 %.0f 行/秒"
            % (scr["iters"], scr["step"], scr["ops"], scr["lf_lines"], scr["lf_lps"]), ""]

    # 7. 吞吐与超长行
    T.section("7. 输出吞吐 — %d 行带颜色/属性文本 (会写入滚动缓冲)" % args.lines)
    thr = bench_throughput(args.lines)
    results["thr"] = thr
    _fmt("耗时", "%.3f 秒" % thr["sec"])
    _fmt("行/秒", "%.0f" % thr["lps"])
    _fmt("MB/秒", "%.2f" % thr["mbps"])
    rep += ["# 吞吐", "%d 行 %.3f 秒 %.0f 行/秒 %.2f MB/秒" % (thr["lines"], thr["sec"],
                                                              thr["lps"], thr["mbps"]), ""]

    T.section("8. 超长行 — 单行 1000000 字符 (自动换行路径)")
    lng = bench_longline(1000000)
    results["long"] = lng
    _fmt("字符数", lng["chars"])
    _fmt("耗时", "%.2f ms" % lng["ms"])
    _fmt("速度", "%.2f MB/s (约 %.0f 个物理行)" % (lng["mbps"], lng["wrapped"]))
    rep += ["# 超长行", "%d 字符 %.3fms %.2f MB/s" % (lng["chars"], lng["ms"], lng["mbps"]), ""]

    # 收尾: 清掉压测留下的滚动缓冲, 再打印汇总 (3J 同时用于清理)
    T.clear_all()

    T.section("汇总表 (完整数字已写入报告文件)")
    _table("延迟 / 帧率", [
        ("RTT avg (ms)", "%.2f" % rtt["avg"] if rtt.get("ok") else "无回包"),
        ("FPS 全量 avg (ms/帧)", "%.3f" % fps_full["avg_ms"]),
        ("FPS 增量 avg (ms/帧)", "%.3f" % fps_incr["avg_ms"]),
        ("FPS 全量 / 增量", "%.2f" % ratio),
        ("真实帧率 全量/增量 (fps)", "%.1f / %.1f" % (fps_full["fps"], fps_incr["fps"])),
    ])
    _table("压力项", [
        ("属性风暴 fps", "%.1f" % storm["fps"]),
        ("属性风暴 写入 (ms/帧)", "%.2f" % storm["write_ms"]),
        ("颜色切换 (ms/帧)", "%.2f" % csw["cell_ms"]),
        ("CSI S/T (次/秒)", "%.1f" % scr["ops"]),
        ("LF 洪水 (行/秒)", "%.0f" % scr["lf_lps"]),
        ("吞吐 (行/秒)", "%.0f" % thr["lps"]),
        ("吞吐 (MB/秒)", "%.2f" % thr["mbps"]),
        ("超长行 1000000 字符 (ms)", "%.2f" % lng["ms"]),
    ])

    # 光标状态一致性检查 (属性风暴之后屏幕是否还能正常定位)
    T.section("判定")
    if pos is not None:
        T.row("风暴后光标定位", T.PASS if pos == (1, 1) else T.WARN,
              "CSI 6n 回包 = %s (期望 (1,1))" % (pos,))
        rep += ["风暴后 CSI 6n 位置=%s (期望 (1,1))" % (pos,)]
    else:
        T.row("风暴后光标定位", T.SKIP, "终端未实现 DSR(6n), 请人工目视是否花屏")
        rep += ["风暴后 CSI 6n: 无回包"]

    if not rtt.get("ok"):
        T.row("往返延迟 RTT", T.SKIP, "终端未响应 CSI 6n, 无法测量按键链路开销")
    elif rtt["avg"] < TH_RTT_OK:
        T.row("往返延迟 RTT", T.PASS, "平均 %.2f ms (<%.0f), 按键链路顺畅" % (rtt["avg"], TH_RTT_OK))
    elif rtt["avg"] < TH_RTT_WARN:
        T.row("往返延迟 RTT", T.WARN, "平均 %.2f ms, 略高 (可能是查询走同步路径)" % rtt["avg"])
    else:
        T.row("往返延迟 RTT", T.WARN, "平均 %.2f ms, 链路开销偏大" % rtt["avg"])

    for tag, r in (("全量重绘", fps_full), ("增量更新", fps_incr)):
        if r["avg_ms"] < TH_FRAME_60FPS:
            T.row("60fps 动画/" + tag, T.PASS, "单帧 %.2f ms (<16.7), 可稳定 60fps" % r["avg_ms"])
        elif r["avg_ms"] < TH_FRAME_30FPS:
            T.row("60fps 动画/" + tag, T.WARN,
                  "单帧 %.2f ms, 达不到 60fps (受限于终端渲染)" % r["avg_ms"])
        else:
            T.row("60fps 动画/" + tag, T.WARN,
                  "单帧 %.2f ms (>33), 疑似未批量提交渲染或终端逐帧同步刷新" % r["avg_ms"])
    if ratio > 1.3:
        T.row("增量更新收益", T.PASS, "单帧耗时降到 1/%.2f (最小更新路径有效)" % ratio)
    else:
        T.row("增量更新收益", T.WARN,
              "仅 %.2fx, CUP 绝对定位本身有固定开销, 小改动不一定比重绘划算" % ratio)

    if storm["fps"] >= TH_STORM_FPS_OK:
        T.row("属性风暴", T.PASS, "%.1f fps, truecolor 全屏刷帧无压力" % storm["fps"])
    elif storm["fps"] >= TH_STORM_FPS_WARN:
        T.row("属性风暴", T.WARN, "%.1f fps, truecolor 刷帧偏慢 (必要时目视花屏/撕裂)"
              % storm["fps"])
    else:
        T.row("属性风暴", T.WARN, "%.1f fps 很低, 疑似未批量提交真彩色属性" % storm["fps"])

    T.row("颜色切换压力", T.PASS if csw["cell_ms"] < TH_FRAME_30FPS else T.WARN,
          "全屏每格换 SGR 单帧 %.2f ms, %.1f µs/格" % (csw["cell_ms"], csw["per_cell_us"]))

    if scr["ops"] >= TH_SCROLL_OPS_OK:
        T.row("CSI S/T 滚动", T.PASS, "%.0f 次/秒" % scr["ops"])
    else:
        T.row("CSI S/T 滚动", T.WARN, "%.0f 次/秒, 大范围滚动偏慢 (可能整屏重排)" % scr["ops"])
    T.row("LF 洪水滚动", T.PASS if scr["lf_lps"] >= TH_LF_OK else T.WARN,
          "%.0f 行/秒" % scr["lf_lps"])

    if thr["lps"] >= TH_LPS_OK:
        T.row("输出吞吐", T.PASS, "%.0f 行/秒, %.2f MB/秒" % (thr["lps"], thr["mbps"]))
    elif thr["lps"] >= TH_LPS_WARN:
        T.row("输出吞吐", T.WARN, "%.0f 行/秒, 偏低 (终端渲染慢或未批量提交)" % thr["lps"])
    else:
        T.row("输出吞吐", T.WARN,
              "%.0f 行/秒 很低, 疑似未批量提交渲染 (逐行 flush) 或终端滚动缓冲代价高" % thr["lps"])

    T.row("超长行自动换行", T.PASS if lng["mbps"] >= TH_MBPS_OK else T.WARN,
          "100 万字符 %.2f ms (%.2f MB/s), 约 %.0f 个物理行" % (lng["ms"], lng["mbps"], lng["wrapped"]))

    path = T.save_report("t_perf", "\n".join(rep) + "\n")
    if path:
        T.hint("数字报告: " + T.safe_path(path))
    T.finish("t_perf 结束")
    return 0


if __name__ == "__main__":
    sys.exit(T.guard(main))