# -*- coding: utf-8 -*-
"""t_tui.py — 交互式 TUI 贪吃蛇: 检验终端的"手感"

检验范围:
  * 备用屏 (CSI ?1049h/l) 进入与退出、光标隐藏与恢复、窗口标题
  * 在自适应窗口尺寸下用 CUP 绝对定位绘制 ASCII 棋盘
  * 方向键两种形态 (CSI ESC[A-D 与 DECCKM 应用态 ESC OA-OD) 与 WASD 输入
  * 两种渲染策略实时切换: 全量重绘 / 增量更新 (只改变化格, 最小更新路径)
  * 约 20Hz 高频重绘下的单帧耗时 (平均/最大)、实际帧率
  * 按键 → 屏幕重绘的端到端延迟
  * 暂停 / 调速 / 死亡重开等状态切换
  * 退出原因回显 (退出键 / stdin 到 EOF), 免得"突然结束"看不出是谁干的

本程序检验的是"手感": 输入延迟、闪烁与撕裂、高频重绘是否掉帧。
棋盘只用 ASCII 字符 ('@' 头 / 'o' 身 / '*' 食物 / '#' 墙), 避免与字符宽度
问题混淆 —— 宽度/宽字符是 t_unicode.py 的职责。

非交互模式 (TERMTEST_NONINTERACTIVE=1):
    无人操作, 由内置脚本自动玩 40 帧 (前 20 帧走全量重绘, 后 20 帧自动切到
    增量更新), 覆盖两条渲染路径后打印统计并以退出码 0 结束。
"""

import random
import sys
import time
from collections import deque

import termlib as T

TICK_BASE = 0.05          # 基准节拍 20Hz
TICK_MIN = 0.015
TICK_MAX = 0.25
HUD_INTERVAL = 0.4        # 状态行最多 2.5Hz 刷新, 保证增量帧只画棋盘差异
AUTO_FRAMES = 40          # 非交互模式自动帧数
INPUT_POLL = 0.02         # 每次输入轮询的等待上限 (到点就回循环, 好让帧照画)
INPUT_GAP = 0.01          # 同一批里把字节并成一块的间隔; 序列被拆开的重拼由
                          # termlib.read_chunk 的 PARTIAL_WAIT 兜底, 不靠这里

CH_HEAD, CH_BODY, CH_FOOD, CH_WALL = "@", "o", "*", "#"
ST_HEAD = T.sgr(1, 92)
ST_BODY = T.sgr(92)
ST_FOOD = T.sgr(1, 91)
ST_WALL = T.sgr(90)
_STY = {CH_HEAD: ST_HEAD, CH_BODY: ST_BODY, CH_FOOD: ST_FOOD, CH_WALL: ST_WALL}

ARROWS = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
          b"\x1bOA": "up", b"\x1bOB": "down", b"\x1bOC": "right", b"\x1bOD": "left"}
DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
_KEYS = {b"w": "up", b"W": "up", b"a": "left", b"A": "left",
         b"s": "down", b"S": "down", b"d": "right", b"D": "right",
         b"p": "pause", b"P": "pause", b"q": "quit", b"Q": "quit",
         b"r": "mode", b"R": "mode", b"+": "fast", b"=": "fast",
         b"-": "slow", b"_": "slow", b"\x03": "quit", b"\x1b": "quit"}


# ------------------------------------------------------------------ 排版助手
def _fit(s, width):
    """按显示宽度截断并补齐到 width (含中文的提示行用)。"""
    if width <= 0:
        return ""
    if T.dwidth(s) <= width:
        return T.pad(s, width)
    out, w = [], 0
    for ch in s:
        cw = T.dwidth(ch)
        if w + cw > width:
            break
        out.append(ch)
        w += cw
    return "".join(out) + " " * (width - w)


def _layout():
    """根据窗口尺寸决定棋盘大小与左上角。返回 (bw, bh, r0, c0, cols, rows)。"""
    cols, rows = T.term_size()
    bw = max(16, min(cols - 6, 78))
    bh = max(6, min(rows - 8, 28))
    return bw, bh, 4, 2, cols, rows


# ------------------------------------------------------------------ 输入解析
def _key_action(k):
    a = ARROWS.get(k)
    if a:
        return a
    return _KEYS.get(k)


# ------------------------------------------------------------------ 渲染
def _row_text(line):
    """把一行格子转成带最少 SGR 切换的字符串。"""
    out, cur = [], ""
    for ch in line:
        s = _STY.get(ch, "")
        if s != cur:
            out.append(s if s else T.sgr(0))
            cur = s
        out.append(ch)
    if cur:
        out.append(T.sgr(0))
    return "".join(out)


def _render_full(grid, bw, bh, r0, c0):
    """全量重绘: 每帧重写棋盘区域所有格子。"""
    out = [T.cup(r0, c0) + ST_WALL + "#" * (bw + 2) + T.sgr(0)]
    for y in range(bh):
        out.append(T.cup(r0 + 1 + y, c0) + ST_WALL + "#" + T.sgr(0))
        out.append(_row_text(grid[y]))
        out.append(ST_WALL + "#" + T.sgr(0))
    out.append(T.cup(r0 + bh + 1, c0) + ST_WALL + "#" * (bw + 2) + T.sgr(0))
    return "".join(out)


def _render_diff(prev, grid, bw, bh, r0, c0):
    """增量更新: 只对发生变化的连续格子序列做 CUP 定位重写。"""
    out = []
    for y in range(bh):
        p, n = prev[y], grid[y]
        x = 0
        while x < bw:
            if p[x] == n[x]:
                x += 1
                continue
            sty = _STY.get(n[x], "")
            start = x
            x += 1
            while x < bw and n[x] != p[x] and _STY.get(n[x], "") == sty:
                x += 1
            out.append(T.cup(r0 + 1 + y, c0 + 1 + start))
            out.append(sty if sty else T.sgr(0))
            out.append("".join(n[start:x]))
            if sty:
                out.append(T.sgr(0))
    return "".join(out)


# ------------------------------------------------------------------ 游戏逻辑
def _place_food(g, bw, bh, rng):
    occupied = set(g["snake"])
    free = [(x, y) for y in range(bh) for x in range(bw) if (x, y) not in occupied]
    if not free:
        return None
    return rng.choice(free)


def _new_game(bw, bh, rng):
    cx, cy = bw // 2, bh // 2
    g = {"snake": deque([(cx, cy), (cx - 1, cy), (cx - 2, cy)]),
         "dir": (1, 0), "food": None, "score": 0, "alive": True}
    g["food"] = _place_food(g, bw, bh, rng)
    return g


def _build_grid(g, bw, bh):
    grid = [[" "] * bw for _ in range(bh)]
    if g["food"] is not None:
        fx, fy = g["food"]
        grid[fy][fx] = CH_FOOD
    for i, (x, y) in enumerate(g["snake"]):
        grid[y][x] = CH_HEAD if i == 0 else CH_BODY
    return grid


def _step(g, bw, bh, rng):
    """走一步。撞墙/撞自己 → 死亡并返回 False。"""
    dx, dy = g["dir"]
    hx, hy = g["snake"][0]
    nx, ny = hx + dx, hy + dy
    if not (0 <= nx < bw and 0 <= ny < bh):
        g["alive"] = False
        return False
    body = set(g["snake"])
    body.discard(g["snake"][-1])          # 尾格会腾出来
    if (nx, ny) in body:
        g["alive"] = False
        return False
    g["snake"].appendleft((nx, ny))
    if g["food"] is not None and (nx, ny) == g["food"]:
        g["score"] += 1
        g["food"] = _place_food(g, bw, bh, rng)
        return True
    g["snake"].pop()
    return True


def _ai_dir(g, bw, bh):
    """非交互模式的简易寻食策略: 在安全方向里挑离食物最近的。"""
    hx, hy = g["snake"][0]
    fx, fy = g["food"] if g["food"] is not None else (hx, hy)
    body = set(g["snake"])
    body.discard(g["snake"][-1])
    cands = []
    for name, (dx, dy) in DIRS.items():
        nx, ny = hx + dx, hy + dy
        if not (0 <= nx < bw and 0 <= ny < bh):
            continue
        if (nx, ny) in body:
            continue
        cands.append((abs(nx - fx) + abs(ny - fy), name))
    cands.sort()
    cur = g["dir"]
    for _, name in cands:
        d = DIRS[name]
        if len(g["snake"]) > 1 and (d[0] + cur[0], d[1] + cur[1]) == (0, 0):
            continue
        return d
    return DIRS[cands[0][1]] if cands else cur


# ------------------------------------------------------------------ 主循环
def _play(interactive, frame_budget=0, seed=None):
    bw, bh, r0, c0, cols, rows = _layout()
    hud_w = max(10, cols - 2)
    rng = random.Random(seed)
    g = _new_game(bw, bh, rng)
    grid = _build_grid(g, bw, bh)
    prev = [row[:] for row in grid]

    mode = "full"
    tick = TICK_BASE
    paused = False
    msg = ""
    deaths = 0
    st = {"frames": 0, "full_n": 0, "full_ms": 0.0, "incr_n": 0, "incr_ms": 0.0,
          "max_ms": 0.0, "lats": [], "exit_reason": "",
          "unknown_n": 0, "unknown_ex": []}      # 收到但认不出的完整序列 (鼠标报文/功能键…)
    # 统一用 time.perf_counter(): Windows 上 time.monotonic() 走 GetTickCount64,
    # 分辨率只有 ~15.6ms, 会把单帧耗时全部量化成 0 或 15.6ms, 无法用于性能判定。
    t_start = time.perf_counter()
    next_tick = t_start
    hud_next = 0.0

    if cols < 46 or rows < 14:
        T.note("窗口偏小 (%dx%d), 棋盘已按最小尺寸绘制" % (cols, rows))

    def do_render():
        """渲染一帧并返回本帧耗时 (ms)。"""
        nonlocal grid, prev
        t = time.perf_counter()
        grid = _build_grid(g, bw, bh)
        if mode == "full":
            T.write(_render_full(grid, bw, bh, r0, c0))
        else:
            T.write(_render_diff(prev, grid, bw, bh, r0, c0))
        prev = [row[:] for row in grid]
        dt = (time.perf_counter() - t) * 1000.0
        st["frames"] += 1
        st["max_ms"] = max(st["max_ms"], dt)
        if mode == "full":
            st["full_n"] += 1
            st["full_ms"] += dt
        else:
            st["incr_n"] += 1
            st["incr_ms"] += dt
        return dt

    def draw_hud():
        elapsed = max(1e-6, time.perf_counter() - t_start)
        avg = (st["full_ms"] + st["incr_ms"]) / max(1, st["frames"])
        lat = "%.1f ms" % st["lats"][-1] if st["lats"] else "—"
        lines = [
            "贪吃蛇  |  渲染: %s (r 切换)  |  速度: %d Hz (+/-)"
            % ("全量重绘" if mode == "full" else "增量更新", int(round(1.0 / tick))),
            "帧数 %d   平均 %.2f ms   最大 %.2f ms   实际 %.1f fps   按键→重绘 %s"
            % (st["frames"], avg, st["max_ms"], st["frames"] / elapsed, lat),
            "方向键 / WASD 移动   p 暂停   r 切换渲染   +/- 调速   q 退出",
        ]
        out = []
        for i, s in enumerate(lines):
            out.append(T.cup(1 + i, 1) + T.sgr(0) + _fit(s, hud_w))
        T.write("".join(out))

    def draw_msg(text, style=""):
        T.write(T.cup(r0 + bh + 2, c0) + T.sgr(0) + style + _fit(text, hud_w) + T.sgr(0))

    T.alt_on()
    T.show_cursor(False)
    T.set_title("termtest · t_tui.py 贪吃蛇")
    T.write(T.sgr(0))
    draw_hud()
    do_render()
    st["frames"] = 0
    st["full_n"] = 0
    st["full_ms"] = 0.0
    hud_next = time.perf_counter() + HUD_INTERVAL

    cm = T.Raw() if interactive else None
    if cm is not None:
        cm.__enter__()
    inp = T.InputReader(gap=INPUT_GAP)
    last_msg = None
    try:
        while True:
            key_time = None
            need_render = False
            quit_now = False

            if interactive:
                keys = inp.read(timeout=INPUT_POLL)
                if keys:
                    hit = False                # 本批里有没有"认得出"的按键
                    for k in keys:
                        act = _key_action(k)
                        if act is None:
                            # 认不出的**完整**序列 (鼠标报文、Home/End、Ctrl+方向键、
                            # kitty CSI-u…) 当没看见: 既不退出, 也不计进按键统计;
                            # 但要记下来 —— "终端背着我发了什么"正是这个测试要暴露的事。
                            st["unknown_n"] += 1
                            # 只记终端主动发的序列 (ESC 开头) 的按键名; 用户随手敲的
                            # 普通字符只计数不回显, 免得报告带上击键内容
                            if len(st["unknown_ex"]) < 2 and k[:1] == b"\x1b":
                                st["unknown_ex"].append(T.key_name(k))
                            continue
                        if not hit:
                            hit = True
                            key_time = time.perf_counter()   # 延迟从认出来那一刻算
                        if act == "quit":
                            st["exit_reason"] = "收到退出键 %s" % T.key_name(k)
                            quit_now = True
                            break
                        if act == "pause":
                            paused = not paused
                            msg = "已暂停 — 按 p 继续" if paused else ""
                            need_render = True
                        elif act == "mode":
                            mode = "incr" if mode == "full" else "full"
                            msg = "渲染模式: %s" % ("全量重绘" if mode == "full" else "增量更新")
                            need_render = True
                        elif act == "fast":
                            tick = max(TICK_MIN, tick * 0.8)
                        elif act == "slow":
                            tick = min(TICK_MAX, tick * 1.25)
                        elif act in DIRS:
                            d = DIRS[act]
                            cur = g["dir"]
                            if len(g["snake"]) <= 1 or (d[0] + cur[0], d[1] + cur[1]) != (0, 0):
                                g["dir"] = d
                    if quit_now:
                        break
                    need_render = hit          # 只有认出来的键值得重画一帧 (鼠标噪声不重画)
                elif not T.input_alive():
                    # 这句只写在备用屏里, 退出时会随 1049l 一起被擦掉 —— 让人真看见的是
                    # st["exit_reason"], 由 _report 在退出备用屏之后打印。
                    st["exit_reason"] = "stdin 已到 EOF (输入通道被关闭), 收不到按键"
                    break

            if not g["alive"]:
                do_render()
                draw_hud()
                draw_msg("游戏结束! 得分 %d — 按任意键重开 (q 退出)" % g["score"], T.sgr(1, 31))
                if not interactive:
                    st["exit_reason"] = "非交互模式: 撞死即结束"
                    break
                T.write(T.cup(r0 + bh + 3, c0))
                try:
                    k = T.wait_key("按任意键重开新局 (q 退出)")
                except T.QuitTest:
                    # wait_key 在"按了退出键"和"stdin 到 EOF"两种情况下都抛 QuitTest,
                    # 用 input_alive() 分开写, 免得把 EOF 记成"用户按了退出键"。
                    st["exit_reason"] = ("游戏结束后 stdin 到 EOF" if not T.input_alive()
                                         else "游戏结束后按了退出键 (q/Esc/Ctrl+C)")
                    break
                if not k:
                    st["exit_reason"] = "游戏结束后 stdin 到 EOF"
                    break
                deaths += 1
                g = _new_game(bw, bh, rng)
                grid = _build_grid(g, bw, bh)
                prev = [row[:] for row in grid]
                msg = ""
                do_render()
                next_tick = time.perf_counter() + tick
                continue

            now = time.perf_counter()
            do_tick = (not paused) and now >= next_tick
            if do_tick:
                if not interactive:
                    g["dir"] = _ai_dir(g, bw, bh)
                _step(g, bw, bh, rng)
                next_tick = now + tick

            if do_tick or need_render:
                do_render()
                if key_time is not None:
                    st["lats"].append((time.perf_counter() - key_time) * 1000.0)
                if frame_budget and not interactive and st["frames"] == frame_budget // 2:
                    mode = "incr"          # 自动覆盖第二条渲染路径
                if msg != last_msg:      # 消息变化时才写, 清空消息写成空格
                    draw_msg(msg)
                    last_msg = msg
                if time.perf_counter() >= hud_next:
                    draw_hud()
                    hud_next = time.perf_counter() + HUD_INTERVAL
                if frame_budget and st["frames"] >= frame_budget:
                    st["exit_reason"] = "非交互模式: 跑满 %d 帧" % frame_budget
                    break
            else:
                # 没有要画的东西: 睡到下一个节拍 (交互模式下 read_chunk 已经等过 20ms,
                # 这里只做很短的让出, 避免空转把 CPU 打满而干扰帧耗时测量)
                remain = next_tick - time.perf_counter()
                if remain > 0.002:
                    time.sleep(min(0.005, remain))
    finally:
        if cm is not None:
            cm.__exit__(None, None, None)
        T.write(T.sgr(0))
        T.show_cursor(True)
        T.alt_off()

    st["elapsed"] = time.perf_counter() - t_start
    st["deaths"] = deaths
    st["tick"] = tick
    return st


# ------------------------------------------------------------------ 报告
def _report(st):
    frames = st["frames"]
    elapsed = st.get("elapsed", 0.0)
    T.wln()
    if st.get("exit_reason"):
        # 必须等退出备用屏之后再打: 在备用屏里打的提示会随 1049l 一起被擦掉,
        # 用户只能看到"玩着玩着突然回到菜单", 压根不知道是谁把游戏结束掉的。
        T.note("退出原因: %s" % st["exit_reason"])
    T.divider("运行统计")
    avg = (st["full_ms"] + st["incr_ms"]) / max(1, frames)
    rows = [
        ("总帧数", "%d" % frames),
        ("运行时长", "%.2f 秒" % elapsed),
        ("平均单帧耗时", "%.2f ms" % avg),
        ("最大单帧耗时", "%.2f ms" % st["max_ms"]),
        ("实际帧率", "%.1f fps" % (frames / elapsed if elapsed > 0 else 0.0)),
        ("全量重绘", "%d 帧 / 平均 %.2f ms" % (st["full_n"], st["full_ms"] / st["full_n"])
         if st["full_n"] else "未测到"),
        ("增量更新", "%d 帧 / 平均 %.2f ms" % (st["incr_n"], st["incr_ms"] / st["incr_n"])
         if st["incr_n"] else "未测到"),
        ("按键→重绘延迟", ("最近 %.1f ms / 平均 %.1f ms / 最大 %.1f ms"
                        % (st["lats"][-1], sum(st["lats"]) / len(st["lats"]), max(st["lats"])))
         if st["lats"] else "无按键输入"),
        ("死亡重开次数", "%d" % st.get("deaths", 0)),
        ("未识别输入", ("%d 条%s" % (
            st["unknown_n"],
            (" (终端序列例: %s)" % "; ".join(st["unknown_ex"])) if st["unknown_ex"] else ""))
         if st.get("unknown_n") else "无"),
    ]
    for k, v in rows:
        T.wln("  " + T.sgr(90) + T.pad(k, 16) + T.sgr(0) + v)

    T.section("判定")
    if frames == 0:
        T.row("帧统计", T.SKIP, "本次未产生任何帧")
        return
    if avg < 20:
        T.row("平均单帧耗时", T.PASS, "%.2f ms (<20ms), 20Hz 重绘余量充足" % avg)
    elif avg <= 33:
        T.row("平均单帧耗时", T.PASS, "%.2f ms, 略高但在 30fps 以内" % avg)
    else:
        T.row("平均单帧耗时", T.WARN, "%.2f ms (>33ms), 可能是终端渲染慢或未批量提交" % avg)
    if st["max_ms"] < 50:
        T.row("最大单帧耗时", T.PASS, "%.2f ms, 无明显卡顿" % st["max_ms"])
    else:
        T.row("最大单帧耗时", T.WARN, "%.2f ms, 存在抖动 (GC 或终端渲染阻塞)" % st["max_ms"])
    fps = frames / elapsed if elapsed > 0 else 0.0
    if fps >= 17:
        T.row("实际帧率", T.PASS, "%.1f fps (目标 ~20)" % fps)
    else:
        T.row("实际帧率", T.WARN, "%.1f fps 低于节拍, 重绘跟不上" % fps)
    if st["lats"]:
        mx = max(st["lats"])
        ref = "平均 %.1f ms / 最大 %.1f ms" % (sum(st["lats"]) / len(st["lats"]), mx)
        if mx < 80:
            T.row("按键→重绘延迟", T.PASS, ref)
        else:
            T.row("按键→重绘延迟", T.WARN, ref + ", 手感会发钝")
    else:
        T.row("按键→重绘延迟", T.SKIP, "非交互/无按键, 未测")
    if st["full_n"] and st["incr_n"]:
        fa = st["full_ms"] / st["full_n"]
        ia = st["incr_ms"] / st["incr_n"]
        if fa < 0.02 and ia < 0.02:
            T.row("增量 vs 全量渲染", T.SKIP,
                  "两者都 <0.02ms (本机渲染极快, 计时接近分辨率), 精确对比见 t_perf.py")
        elif ia < fa:
            T.row("增量 vs 全量渲染", T.PASS,
                  "增量 %.3f ms / 全量 %.3f ms (节省 %.0f%%)" % (ia, fa, (1 - ia / fa) * 100))
        else:
            T.row("增量 vs 全量渲染", T.WARN,
                  "增量 %.3f ms 不低于全量 %.3f ms, CUP 定位本身可能有固定开销" % (ia, fa))
    else:
        T.row("增量 vs 全量渲染", T.SKIP, "本次只覆盖了一种渲染路径 (可运行中按 r 切换)")


def main():
    T.start("t_tui.py",
            ["备用屏+隐藏光标", "ASCII 棋盘自适应尺寸", "方向键 CSI/SS3 + WASD",
             "全量重绘 vs 增量更新", "帧耗时/帧率/按键延迟", "暂停/调速/死亡重开"],
            note="检验\"手感\": 输入延迟、闪烁与撕裂、高频重绘是否掉帧。")
    T.section("贪吃蛇 (方向键/WASD 移动, p 暂停, r 切换渲染, +/- 调速, q 退出)")
    if T.NONINTERACTIVE:
        T.hint("非交互模式: 内置脚本自动玩 %d 帧 (前 %d 帧全量重绘, 后 %d 帧增量更新)"
               % (AUTO_FRAMES, AUTO_FRAMES // 2, AUTO_FRAMES - AUTO_FRAMES // 2))
        st = _play(False, frame_budget=AUTO_FRAMES, seed=271828)
    else:
        T.hint("游戏 2 秒后自动开始, 方向键/WASD 移动, p 暂停, r 切换渲染模式, q 退出")
        st = _play(True)
    _report(st)
    T.finish("t_tui 结束")
    return 0


if __name__ == "__main__":
    sys.exit(T.guard(main))