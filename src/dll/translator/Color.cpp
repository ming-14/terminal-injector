// VT 颜色映射实现：Console 16 色属性 → VT SGR
// 详见 docs/phases/03-dll-framework.md 4.6.2
//
// Windows 颜色位（bit 序为 BGR）：
//   bit 0 (0x1) 前景蓝    bit 4 (0x10) 背景蓝
//   bit 1 (0x2) 前景绿    bit 5 (0x20) 背景绿
//   bit 2 (0x4) 前景红    bit 6 (0x40) 背景红
//   bit 3 (0x8) 前景强度  bit 7 (0x80) 背景强度
// RGB 组合：红+绿=黄，红+蓝=品红，绿+蓝=青，全=白
//
// 缓存：thread_local lastAttr，仅属性变化时输出 SGR（减少字节量）
#include "VtEscape.h"
#include <cstdio>

namespace terminjector::vt {

namespace {

// thread_local 保证多线程安全（文档风险表建议）
thread_local WORD t_lastAttr = 0xFFFF;  // 初始无效值，首次必输出

// 前景 3 位 RGB → VT 30+ 索引（0=黑 1=红 2=绿 3=黄 4=蓝 5=品红 6=青 7=白）
const int kFgMap[8] = {30, 31, 32, 33, 34, 35, 36, 37};

// 背景 3 位 RGB → VT 40+ 索引
const int kBgMap[8] = {40, 41, 42, 43, 44, 45, 46, 47};

// Windows 属性 3 位 BGR（bit0=蓝 bit1=绿 bit2=红）→ VT 索引（bit0=红 bit1=绿 bit2=蓝）
// 直接当索引会导致红/蓝互换（BUG-001 根因），必须重映射
inline int ToVtIndex(WORD bgr) {
    return (int)(((bgr & 0x4) >> 2) | (bgr & 0x2) | ((bgr & 0x1) << 2));
}

} // namespace

std::string SgrFromAttribute(WORD attr) {
    // 属性未变化：不输出（减少 VT 字节）
    if (attr == t_lastAttr) return {};
    t_lastAttr = attr;

    char buf[64];
    int n = 0;

    // 前景
    WORD fg = attr & 0xF;
    // 背景
    WORD bg = (attr >> 4) & 0xF;

    // 构造 SGR：\x1b[<codes>m
    n += std::snprintf(buf + n, sizeof(buf) - n, "%s", kCsi);
    bool first = true;

    auto add = [&](int code) {
        if (!first) n += std::snprintf(buf + n, sizeof(buf) - n, ";");
        n += std::snprintf(buf + n, sizeof(buf) - n, "%d", code);
        first = false;
    };

    // 前景高强度
    if (fg & 0x8) add(1);
    add(kFgMap[ToVtIndex(fg & 0x7)]);

    // 背景高强度（VT 用 5 即闪烁，Windows 语义是高强度背景）
    if (bg & 0x8) add(5);
    add(kBgMap[ToVtIndex(bg & 0x7)]);

    std::snprintf(buf + n, sizeof(buf) - n, "m");
    return std::string(buf);
}

// CursorPosition / ResizeWindow 等非颜色 VT 序列见 VtEscape.cpp

} // namespace terminjector::vt
