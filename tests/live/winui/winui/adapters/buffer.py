"""字符网格缓冲（接口适配层）。

CharBuffer 是 TUI 渲染的中间表示：
  - 以单元格 (字符, 颜色属性) 存储，支持东亚宽字符（占据两个单元格）
  - 持有上一帧快照，可计算差量区域，供驱动层做增量绘制
不依赖任何驱动实现，属性常量引用 entities.theme。
字符显示宽度使用 wcwidth 库（正确处理组合字符等）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from wcwidth import wcwidth

from winui.entities.theme import COMMON_LVB_LEADING_BYTE, COMMON_LVB_TRAILING_BYTE

logger = logging.getLogger("winui.buffer")


@dataclass(slots=True)
class Cell:
    """单个单元格。wide=True 表示该格是宽字符的首格。"""
    ch: str | None
    attr: int = 0
    wide: bool = False


def char_width(ch: str) -> int:
    """字符占用单元格数：东亚宽字符为 2，其余为 1（wcwidth 为 0 的组合字符按 1）。"""
    return max(wcwidth(ch), 1)


def text_width(text: str) -> int:
    """整段文本的显示宽度（单元格数）。"""
    return sum(char_width(ch) for ch in text)


def clip_text(text: str, max_width: int) -> str:
    """按显示宽度截断文本（不会切断宽字符）。"""
    out = ""
    w = 0
    for ch in text:
        w += char_width(ch)
        if w > max_width:
            break
        out += ch
    return out


class CharBuffer:
    """宽 x 高的字符网格，支持文本/块填充与差量提取。"""

    def __init__(self, width: int, height: int) -> None:
        if width < 1 or height < 1:
            raise ValueError(f"非法缓冲区尺寸 {width}x{height}")
        self.w = width
        self.h = height
        self.cells: list[list[Cell]] = [[Cell(" ") for _ in range(width)]
                                        for _ in range(height)]

    # ---- 基础写入 ----
    def put_char(self, x: int, y: int, ch: str, attr: int = 0) -> None:
        """写入单个字符。若落在宽字符配对格上，同时清理配对格。"""
        if not (0 <= x < self.w and 0 <= y < self.h):
            return
        cell = self.cells[y][x]
        if cell.wide and x + 1 < self.w:
            # 覆盖宽字符首格：清掉尾格
            self.cells[y][x + 1] = Cell(" ")
        if ch and char_width(ch) == 2:
            # 写入宽字符：占据两格，首格标记 wide
            if x + 1 >= self.w:
                ch = " "  # 行尾放不下宽字符，降级为空格
            else:
                self.cells[y][x + 1] = Cell(None, attr)
        is_wide = char_width(ch) == 2 and x + 1 < self.w
        self.cells[y][x] = Cell(ch if ch else " ", attr, wide=is_wide)

    def put_text(self, x: int, y: int, text: str, attr: int = 0,
                 clip: bool = True) -> None:
        """自 (x, y) 起逐字符写入文本。clip=False 时超出右侧的内容忽略。"""
        if y < 0 or y >= self.h or not text:
            return
        for ch in text:
            if ch in "\n\r\t":
                continue
            if x >= self.w:
                return
            width = char_width(ch)
            if x + width > self.w:
                if not clip:
                    return
                break
            self.put_char(x, y, ch, attr)
            x += width

    def fill(self, x: int, y: int, w: int, h: int, ch: str = " ", attr: int = 0) -> None:
        """填充矩形区域。"""
        x2, y2 = min(x + w, self.w), min(y + h, self.h)
        for yy in range(max(y, 0), y2):
            for xx in range(max(x, 0), x2):
                if self.cells[yy][xx].wide:
                    # 先清配对格，避免宽字符残留
                    self.clear(xx, yy)
                self.cells[yy][xx] = Cell(ch, attr)

    def clear(self, x: int, y: int) -> None:
        """清空单个单元格，并处理宽字符配对逻辑。"""
        cell = self.cells[y][x]
        if cell.wide and x + 1 < self.w:
            self.cells[y][x + 1] = Cell(" ")
        elif x > 0 and self.cells[y][x - 1].wide:
            # 当前格是宽字符尾格：连首格一起清
            self.cells[y][x - 1] = Cell(" ")
        self.cells[y][x] = Cell(" ")

    # ---- 读取 ----
    def get(self, x: int, y: int) -> Cell:
        return self.cells[y][x]

    # ---- 差量渲染 ----
    def _row_changed_segments(self, y: int) -> list[tuple[int, int]]:
        """第 y 行相对上一帧的变化段 [(x_start, x_end_inc)]，已包含宽字符配对扩展。"""
        segments: list[tuple[int, int]] = []
        x = 0
        prev_cells = self._prev[y]
        cur = self.cells[y]
        while x < self.w:
            if cur[x] == prev_cells[x]:
                x += 1
                continue
            start = x
            while x < self.w and cur[x] != prev_cells[x]:
                # 宽字符尾格必须与首格同段输出
                if x < self.w and cur[x].wide and x + 1 < self.w:
                    x += 1
                x += 1
            # 向前扩展：start 处的宽字符首格属于本段
            while start > 0 and cur[start - 1].wide:
                start -= 1
            segments.append((start, x))
        return segments

    def render_diff(self) -> list[tuple[int, int, int, int, list[list[tuple[str, int]]]]]:
        """返回 [(x, y, w, h, 字符矩阵)] 差量列表，供驱动增量输出。"""
        if not hasattr(self, "_prev"):
            # 首帧：全量渲染
            self._snapshot()
            return [(0, 0, self.w, self.h, self._extract(0, 0, self.w, self.h))]

        regions: list = []
        for y in range(self.h):
            for x_start, x_end in self._row_changed_segments(y):
                regions.append((x_start, y, x_end - x_start, 1,
                                self._extract(x_start, y, x_end - x_start, 1)))
        self._snapshot()
        return regions

    def _snapshot(self) -> None:
        """把当前帧拷贝为“上一帧”，供下次差量比较。"""
        new_prev = []
        for row in self.cells:
            new_prev.append(list(row))
        self._prev = new_prev
        logger.debug("帧快照完成")

    def _extract(self, x: int, y: int, w: int, h: int) -> list[list[tuple[str, int]]]:
        """提取区域为 (字符, 属性) 矩阵；宽字符首格/尾格附加 LVB 标记。"""
        out = []
        for yy in range(y, y + h):
            row = []
            for xx in range(x, x + w):
                cell = self.cells[yy][xx]
                if cell.ch is None:
                    row.append((" ", cell.attr | COMMON_LVB_TRAILING_BYTE))
                elif cell.wide:
                    row.append((cell.ch, cell.attr | COMMON_LVB_LEADING_BYTE))
                else:
                    row.append((cell.ch, cell.attr))
            out.append(row)
        return out