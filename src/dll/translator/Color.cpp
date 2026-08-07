// VT 颜色映射实现：Console 属性 → VT SGR
// 详见 docs/phases/03-dll-framework.md 4.6.2
//
// Windows 颜色位（bit 序为 BGR）：
//   bit 0 (0x1) 前景蓝    bit 4 (0x10) 背景蓝
//   bit 1 (0x2) 前景绿    bit 5 (0x20) 背景绿
//   bit 2 (0x4) 前景红    bit 6 (0x40) 背景红
//   bit 3 (0x8) 前景强度  bit 7 (0x80) 背景强度
//   bit 14 (0x4000) 反显（COMMON_LVB_REVERSE_VIDEO）
//   bit 15 (0x8000) 下划线（COMMON_LVB_UNDERSCORE）
// RGB 组合：红+绿=黄，红+蓝=品红，绿+蓝=青，全=白
//
// BUG-002 修复（SGR 完整状态机）：旧实现只取低 4+4 位颜色，忽略 LVB
// 反显/下划线，且只输出"开启"无"关闭"语义。pwsh 更新横幅在 ConHost
// 里为 0x4007（反显 + 默认色），劫持快照补发后反显丢失、渲染成纯字。
// 现改为完整状态机：
//   - 反显/下划线 → SGR 7/4；属性位关闭时输出 27/24/22
//   - 默认属性 0x07 不输出显式颜色 SGR（保留终端主题默认色）
//   - 背景强度改 100-107（旧映射为 SGR 5 闪烁，语义不符）
//
// 缓存：thread_local lastAttr，仅属性变化时输出 SGR（减少字节量）
#include "VtEscape.h"
#include <cstdio>

namespace terminjector::vt {

namespace {

// thread_local 保证多线程独立（文档风险表建议）
thread_local WORD t_lastAttr = 0xFFFF;  // 初始无效值，首次必输出

// 前景/背景基础色映射（不含强度、默认色）
const int kFgMap[8] = {30, 31, 32, 33, 34, 35, 36, 37};
const int kBgMap[8] = {40, 41, 42, 43, 44, 45, 46, 47};
// 高强度背景：VT 100-107（Windows 高强度背景语义）
const int kBrightBgMap[8] = {100, 101, 102, 103, 104, 105, 106, 107};

// Windows 属性 3 位 BGR（bit0=蓝 bit1=绿 bit2=红）→ VT 索引（bit0=红 bit1=绿 bit2=蓝）
// 直接当索引会导致红/蓝互换（BUG-001 根因），必须重映射
inline int ToVtIndex(WORD bgr) {
    return (int)(((bgr & 0x4) >> 2) | (bgr & 0x2) | ((bgr & 0x1) << 2));
}

// 颜色部分是否为默认（0x07）。ConHost 默认属性语义 = 采用终端主题默认色，
// 不输出显式前景/背景/强度 SGR（反显/下划线仍独立表达）
inline bool IsDefaultColor(WORD attr) {
    return (attr & 0x00FF) == 0x0007;
}

// 该属性的可表达渲染状态（供前后差异计算）
struct SgrState {
    int  fgCode;     // 前景 SGR 码 30-37；-1 = 默认（输出 39）
    int  bgCode;     // 背景 SGR 码 40-47/100-107；-1 = 默认（输出 49）
    bool bold;       // 前景强度 → SGR 1
    bool underline;  // COMMON_LVB_UNDERSCORE → SGR 4
    bool reverse;    // COMMON_LVB_REVERSE_VIDEO → SGR 7
};

SgrState StateFromAttr(WORD attr) {
    SgrState s{};
    s.underline = (attr & 0x8000) != 0;
    s.reverse   = (attr & 0x4000) != 0;
    if (IsDefaultColor(attr)) {
        // 默认属性：前景/背景交给终端主题
        s.fgCode = -1;
        s.bgCode = -1;
        s.bold   = false;
    } else {
        s.fgCode = 30 + ToVtIndex(attr & 0x7);
        const int bgIdx = ToVtIndex((attr >> 4) & 0x7);
        s.bgCode = ((attr >> 4) & 0x8) ? kBrightBgMap[bgIdx] : kBgMap[bgIdx];
        s.bold   = (attr & 0x8) != 0;
    }
    return s;
}

} // namespace

std::string SgrFromAttribute(WORD attr) {
    // 属性未变化：不输出（减少 VT 字节）
    if (attr == t_lastAttr) return {};
    const WORD prevAttr = t_lastAttr;
    t_lastAttr = attr;

    // 前一次状态：0xFFFF 首帧视为全默认（仅补发当前状态）
    const SgrState prev = (prevAttr == 0xFFFF) ? SgrState{} : StateFromAttr(prevAttr);
    const SgrState cur = StateFromAttr(attr);

    char buf[64];
    int n = 0;
    n += std::snprintf(buf + n, sizeof(buf) - n, "%s", kCsi);
    bool first = true;

    auto add = [&](int code) {
        if (!first) n += std::snprintf(buf + n, sizeof(buf) - n, ";");
        n += std::snprintf(buf + n, sizeof(buf) - n, "%d", code);
        first = false;
    };

    // 先关闭前状态开启、当前状态关闭的属性，避免状态泄漏到后续文本
    if (prev.bold && !cur.bold)       add(22);  // 粗体关
    if (prev.underline && !cur.underline) add(24);  // 下划线关
    if (prev.reverse && !cur.reverse) add(27);  // 反显关
    // 再开启当前状态新激活的属性
    if (cur.bold && !prev.bold)       add(1);   // 粗体开
    if (cur.underline && !prev.underline) add(4);   // 下划线开
    if (cur.reverse && !prev.reverse) add(7);   // 反显开
    // 最后补齐前景/背景：值变化（含默认 39/49 与强度导致的 100-107 切换）
    if (cur.fgCode != prev.fgCode) {
        add(cur.fgCode >= 0 ? cur.fgCode : 39);
    }
    if (cur.bgCode != prev.bgCode) {
        add(cur.bgCode >= 0 ? cur.bgCode : 49);
    }

    // 渲染状态无任何变化（如线程首帧即为默认属性 0x07）：不输出空 SGR
    if (first) return {};

    std::snprintf(buf + n, sizeof(buf) - n, "m");
    return std::string(buf);
}

// CursorPosition / ResizeWindow 等非颜色 VT 序列见 VtEscape.cpp

} // namespace terminjector::vt