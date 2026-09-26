# -*- coding: utf-8 -*-
"""t_images.py — 终端图像协议兼容性测试 (SIXEL / kitty graphics / iTerm2 内联图)

检验范围:
  · SIXEL         基本显示、encode_sixel(max_width, max_height) 自适应缩放、
                  图像后光标位置、被后续输出滚动、两图并排、备用屏内显示、上下文字混排
  · kitty graphics 支持探测 (a=q)、RGBA 分块传输 (a=T) + 显示 (a=p)、
                  指定位置放置、删除 (a=d)
  · iTerm2         OSC 1337;File=...;inline=1 内联 PNG

说明:
  · 本文件是套件中唯一允许使用 Pillow 的文件 (用于现场生成测试图)。
  · SIXEL 编码复用上层目录的 sixel_show.encode_sixel。
  · 终端不支持某协议时, 画面出现乱码或缺图属预期。
  · TERMTEST_NONINTERACTIVE=1 时跳过所有等待与图像发送, 仍会完成编码/探测并退出码 0。
"""

import base64
import io
import os
import re
import sys
import tempfile

from PIL import Image, ImageDraw, ImageFont

import termlib as T

# 复用上层目录的 sixel_show.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from sixel_show import encode_sixel
    SIXEL_OK = True
    SIXEL_ERR = ""
except Exception as _e:                      # noqa: BLE001
    encode_sixel = None
    SIXEL_OK = False
    SIXEL_ERR = repr(_e)


# ---------------------------------------------------------------- 现场生成测试图
def _make_image(w=320, h=220):
    """色条 + 渐变 + 网格 + 大字 'termtest'。"""
    im = Image.new("RGB", (w, h), (0, 0, 0))
    d = ImageDraw.Draw(im)
    bars = [(255, 0, 0), (255, 255, 0), (0, 255, 0), (0, 255, 255),
            (0, 0, 255), (255, 0, 255), (255, 255, 255), (64, 64, 64)]
    bw = w // len(bars)
    for i, c in enumerate(bars):
        d.rectangle([i * bw, 0, (i + 1) * bw, h // 3], fill=c)
    for x in range(w):                        # 水平渐变
        v = int(255 * x / w)
        d.line([(x, h // 3), (x, 2 * h // 3)], fill=(v, 128, 255 - v))
    for x in range(0, w, 20):                 # 网格
        d.line([(x, 2 * h // 3), (x, h)], fill=(90, 90, 90))
    for y in range(2 * h // 3, h, 20):
        d.line([(0, y), (w, y)], fill=(90, 90, 90))
    font = None
    # 跨平台兜底字体候选, 仅用于现场绘制测试图; 逐个尝试失败则退回 PIL 内置字体,
    # 候选名单本身不携带任何本机信息。
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            font = ImageFont.truetype(name, 40)
            break
        except Exception:                     # noqa: BLE001
            font = None
    if font is None:
        try:
            font = ImageFont.load_default(40)
        except Exception:                     # noqa: BLE001
            font = ImageFont.load_default()
    d.text((12, 2 * h // 3 + 8), "termtest", fill=(255, 230, 0), font=font)
    return im


def _save_temp(im):
    """测试图写进项目 _reports/ (已被 .gitignore 排除)。

    不再写共享临时目录: 固定可预测的文件名放在多用户共享目录里可被抢占/预置,
    且进程退出后无人清理会一直残留。
    """
    d = os.path.join(T.HERE, "_reports")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:                     # noqa: BLE001
        d = None                          # _reports 不可写时退回系统临时目录 (mkstemp 随机名, 无抢占风险)
    fd, p = tempfile.mkstemp(prefix="termtest_img_", suffix=".png", dir=d)
    os.close(fd)
    im.save(p)
    return p


def _sixel_dims(data):
    """从 SIXEL 序列头 '1;1;W;H' 读取输出像素尺寸。"""
    m = re.search(r'"1;1;(\d+);(\d+)', data.decode("ascii", "replace"))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _judge(name, a, ok_text, bad_text):
    if a is True:
        T.row(name, T.PASS, ok_text)
    elif a is False:
        T.row(name, T.DIFF, bad_text)
    else:
        T.row(name, T.SKIP, "未作答 / 非交互模式")


# ---------------------------------------------------------------- SIXEL
def _sec_sixel(im, rep):
    T.section("SIXEL · 基本显示 / 缩放 / 光标 / 滚动 / 并排 / 备用屏 / 混排")
    if not SIXEL_OK:
        T.note("导入 sixel_show.encode_sixel 失败: %s" % SIXEL_ERR)
        T.note("请确认上层目录中存在 sixel_show.py")
        T.row("SIXEL 编码器", T.FAIL, "encode_sixel 导入失败")
        return
    T.row("SIXEL 编码器", T.PASS, "已从上层目录导入 encode_sixel")
    T.hint("若终端不支持 SIXEL, 图像位置会显示乱码 —— 属预期。")
    orig = im.size

    # 1) 基本显示
    d = encode_sixel(im, 200, None)
    rep.append("SIXEL 基本显示: %d 字节, 尺寸 %s" % (len(d), _sixel_dims(d)))
    if T.NONINTERACTIVE:
        T.row("SIXEL 基本显示", T.SKIP, "非交互模式: 跳过显示 (%d 字节)" % len(d))
    else:
        T.write(d); T.write(b"\n")
        a = T.ask_yn("是否显示出彩色测试图 (色条 + 渐变 + 网格 + 大字 termtest)?")
        _judge("SIXEL 基本显示", a, "显示正常", "未显示 / 乱码 (可能不支持 SIXEL)")

    # 2) 自适应缩放
    small = encode_sixel(im, 120, 80)
    dims = _sixel_dims(small)
    if dims and dims[0] <= 120 and dims[1] <= 80 and dims != orig:
        T.row("SIXEL 自适应缩放", T.PASS,
              "原图 %dx%d -> 输出 %dx%d" % (orig[0], orig[1], dims[0], dims[1]))
    else:
        T.row("SIXEL 自适应缩放", T.DIFF,
              "输出尺寸 %s (上限 120x80, 原图 %dx%d)" % (dims, orig[0], orig[1]))
    if T.NONINTERACTIVE:
        T.row("SIXEL 缩放显示", T.SKIP, "非交互模式: 跳过显示")
    else:
        T.write(small); T.write(b"\n")
        a = T.ask_yn("图像是否已按上限缩小且未变形?")
        _judge("SIXEL 缩放显示", a, "缩放正常", "缩放异常")

    # 3) 图像后继续打印文本 (光标位置)
    if T.NONINTERACTIVE:
        T.row("SIXEL 图像后光标", T.SKIP, "非交互模式: 跳过显示")
    else:
        T.write(encode_sixel(im, 160, None)); T.write(b"\n")
        T.wln("  [图像后文本] 应出现在图像下方, 说明光标已正确下移。")
        a = T.ask_yn("图像后光标位置是否正确 (文本位于图像下方)?")
        _judge("SIXEL 图像后光标", a, "位置正确", "位置异常 / 覆盖了图像")

    # 4) 图像被后续输出滚动
    if T.NONINTERACTIVE:
        T.row("SIXEL 滚动", T.SKIP, "非交互模式: 跳过显示")
    else:
        T.write(encode_sixel(im, 160, None)); T.write(b"\n")
        for i in range(10):
            T.wln("  [滚动填充 %02d] 把上方图像滚出屏幕, 图像应整体上移, 无残留。" % (i + 1))
        a = T.ask_yn("图像是否随输出正常上移 / 滚动?")
        _judge("SIXEL 滚动", a, "滚动正常", "滚动异常 / 有残留")

    # 5) 两图并排
    if T.NONINTERACTIVE:
        T.row("SIXEL 两图并排", T.SKIP, "非交互模式: 跳过显示")
    else:
        T.hint("两图并排: 左图后不换行直接写右图, 观察是否横向排列。")
        T.write(encode_sixel(im, 80, 80)); T.write(b" ")
        T.write(encode_sixel(im, 80, 80)); T.write(b"\n")
        a = T.ask_yn("两幅图是否并排且不重叠?")
        _judge("SIXEL 两图并排", a, "并排正常", "未并排 / 重叠")

    # 6) 备用屏内显示
    if T.NONINTERACTIVE:
        T.row("SIXEL 备用屏", T.SKIP, "非交互模式: 跳过显示")
    else:
        T.hint("在备用屏中显示图像, 按任意键返回主屏。")
        T.alt_on()
        T.write(encode_sixel(im, 200, None)); T.write(b"\n")
        T.wln("  [备用屏] 图像显示在备用屏内, 退出后原画面应恢复。")
        T.wait_key()
        T.alt_off()
        a = T.ask_yn("备用屏内是否正常显示, 退出后原画面恢复?")
        _judge("SIXEL 备用屏", a, "正常", "异常")

    # 7) 图像上下各一行文字混排
    if T.NONINTERACTIVE:
        T.row("SIXEL 文字混排", T.SKIP, "非交互模式: 跳过显示")
    else:
        T.wln("  === 图像上方文字 ===")
        T.write(encode_sixel(im, 200, None)); T.write(b"\n")
        T.wln("  === 图像下方文字 ===")
        a = T.ask_yn("图像上下的文字是否都与图像正确混排?")
        _judge("SIXEL 文字混排", a, "混排正常", "混排异常")


# ---------------------------------------------------------------- kitty graphics
def _kitty_transmit(im, img_id, chunk=4096):
    """RGBA 原始像素 base64 分块传输: 首块 a=T,f=32,s=W,v=H,i=ID,m=1 ... 末块 m=0。"""
    rgba = im.convert("RGBA")
    w, h = rgba.size
    b64 = base64.b64encode(rgba.tobytes())
    out = []
    first = True
    i = 0
    n = len(b64)
    while i < n:
        piece = b64[i:i + chunk]
        i += chunk
        more = 1 if i < n else 0
        if first:
            ctrl = "a=T,f=32,s=%d,v=%d,i=%d,m=%d" % (w, h, img_id, more)
            first = False
        else:
            ctrl = "m=%d" % more
        out.append(T.APC + "G" + ctrl + ";" + piece.decode("ascii") + T.ST)
    return "".join(out)


def _sec_kitty(im, rep):
    T.section("kitty graphics protocol")
    T.hint("控制序列为 ESC _ G<参数>;<base64载荷> ESC \\。")
    probe = T.APC + "Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA" + T.ST
    r = T.query(probe, timeout=0.5)
    rep.append("kitty 支持探测 (a=q) -> " + (T.dump_bytes(r) if r else "(无响应)"))
    supported = bool(r and b"31;OK" in r)
    if supported:
        T.row("kitty 支持探测", T.PASS, "回包 " + T.dump_bytes(r))
    else:
        T.row("kitty 支持探测", T.SKIP, "无 APC G i=31;OK 回包, 该终端不实现")

    payload = _kitty_transmit(im, 1)
    rep.append("kitty 传输 i=1: 分块序列 %d 字节" % len(payload))

    if T.NONINTERACTIVE:
        T.row("kitty 图像显示", T.SKIP, "非交互模式: 跳过发送")
        T.row("kitty 指定位置放置", T.SKIP, "非交互模式: 跳过发送")
        T.row("kitty 删除图像", T.SKIP, "非交互模式: 跳过发送")
        return

    T.write(payload)
    T.write(T.APC + "Ga=p,i=1" + T.ST)
    T.wln("")
    a = T.ask_yn("kitty 图像是否显示在光标处?")
    _judge("kitty 图像显示", a, "显示正常", "未显示 (可能不支持 kitty 协议)")

    T.write(T.cup(6, 12))
    T.write(T.APC + "Ga=p,i=1,c=12,r=6" + T.ST)
    a = T.ask_yn("图像是否被放置到第 6 行第 12 列?")
    _judge("kitty 指定位置放置", a, "位置正确", "位置异常")

    T.write(T.APC + "Ga=d,i=1" + T.ST)
    a = T.ask_yn("图像 1 是否已被删除?")
    _judge("kitty 删除图像", a, "删除正常", "未删除")


# ---------------------------------------------------------------- iTerm2 内联图
def _sec_iterm(im, rep):
    T.section("iTerm2 内联图像 (OSC 1337)")
    T.hint("序列为 OSC 1337;File=name=<base64文件名>;inline=1;width=40;height=20:<base64数据> BEL。")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    png = buf.getvalue()
    b64 = base64.b64encode(png).decode("ascii")
    name = base64.b64encode(b"termtest.png").decode("ascii")
    seq = (T.OSC + "1337;File=name=%s;inline=1;width=40;height=20:%s" % (name, b64) + T.BEL)
    rep.append("iTerm2 OSC 1337: PNG %d 字节 -> 序列 %d 字节" % (len(png), len(seq)))
    if T.NONINTERACTIVE:
        T.row("iTerm2 内联图像", T.SKIP, "非交互模式: 跳过发送 (PNG %d 字节)" % len(png))
        return
    T.write(seq + "\n")
    a = T.ask_yn("是否显示 PNG 图像, 且尺寸约 40x20 字符?")
    _judge("iTerm2 内联图像", a, "显示正常", "未显示 (可能不支持 OSC 1337)")


# ---------------------------------------------------------------- 收尾
def _cleanup():
    """删除所有 kitty 图像, 复位光标。"""
    T.write(T.APC + "Ga=d,d=A" + T.ST)
    T.show_cursor(True)
    _cols, rows = T.term_size()
    # 停到倒数第二行: 直接停末行的话, 随后这行提示的换行会滚动整屏 (图像测试里
    # 容易被看成"显示异常"), 停在倒数第二行则换行正好落在末行, 不滚动。
    T.park_cursor(max(1, rows - 1))
    T.hint("已发送 kitty 删除全部图像 (a=d,d=A) 并复位光标。")


def main():
    T.start("t_images.py",
            ["SIXEL 显示/缩放", "SIXEL 光标/滚动/并排", "备用屏+文字混排",
             "kitty graphics", "iTerm2 OSC 1337"],
            note="检验终端图像协议; 不支持对应协议时显示乱码或缺图属预期")
    rep = []
    try:
        im = _make_image()
    except Exception as e:                    # noqa: BLE001
        T.note("生成测试图失败: %r" % e)
        T.row("测试图生成", T.FAIL, repr(e))
        T.finish()
        return
    p = _save_temp(im)
    T.hint("测试图已保存到: %s (%dx%d)" % (T.safe_path(p), im.size[0], im.size[1]))
    T.row("测试图生成", T.PASS, os.path.basename(p))

    # 三个协议各占一节; 影像会留在终端的图像层里, 进程退出也不消失 (?1049l 擦不掉),
    # 所以清理必须走 finally: 中途按 q 退出也要把图删掉, 否则会浮在后续测试的画面上
    try:
        _sec_sixel(im, rep)
        _sec_kitty(im, rep)
        _sec_iterm(im, rep)
    finally:
        try:
            _cleanup()
        except Exception as e:                   # noqa: BLE001
            T.note("清理失败: %r" % e)

    body = "t_images.py 图像协议原始记录\n" + "=" * 48 + "\n" + "\n".join(rep) + "\n"
    sp = T.save_report("t_images", body)
    if sp:
        T.hint("原始记录已写入报告: %s" % sp)
    else:
        T.note("报告写入失败")
    T.finish()


if __name__ == "__main__":
    sys.exit(T.guard(main))