// ConsoleToVt 实现：Console API → VT 序列翻译
// 详见 docs/phases/03-dll-framework.md 4.6.3 与 docs/phases/04-output-chain.md
//
// Phase 3：WriteConsoleW 分支（文本透传 + SGR 颜色）
// Phase 4：扩展 FillConsoleOutputCharacter/Attribute、WriteConsoleOutput/Character、
//          ScrollConsoleScreenBuffer 翻译
//
// 翻译原则：
//   1. 坐标转换：Windows 0-based → VT 1-based（行+1, 列+1）
//   2. 颜色变化时输出 SGR（由 SgrFromAttribute thread_local 缓存优化）
//   3. WriteConsoleOutput 跳过空格+默认属性 cell，同行连续同属性合并
//   4. ScrollConsoleScreenBuffer 本 Phase 简化为整体滚屏，Phase 10 补区域裁剪
#include "ConsoleToVt.h"
#include "VtEscape.h"
#include "../state/ConsoleState.h"
#include "logging/Logger.h"

#include <vector>
#include <cstring>

namespace terminjector {

// ===== Phase 10 任务6：WriteConsoleOutput diff 缓存 =====
// 缓存上次 WriteConsoleOutput 写入的 cell 矩阵，下次同 region 调用时
// 仅输出变化的 cell，大幅减少满屏重绘场景的 VT 字节量
//
// 线程安全：
//   - s_cacheLock 保护 s_lastBuffer/s_lastRegion/s_cacheValid
//   - WriteConsoleOutput 可能被多个 Hook 线程并发调用（罕见，但理论上可能）
//   - InvalidateOutputCache 在其他 Hook 路径调用，需与 WriteConsoleOutput 互斥
//
// 缓存失效时机：
//   - FillConsoleOutputCharacter/Attribute：cls/改颜色改变屏幕内容
//   - WriteConsoleOutputCharacter：局部文本覆盖
//   - ScrollConsoleScreenBuffer：滚屏使 cell 偏移
//   - region 变化：WriteConsoleOutput 内部检测到 region 不同时自动全量输出
namespace {
SRWLOCK s_cacheLock = SRWLOCK_INIT;
std::vector<CHAR_INFO> s_lastBuffer;  // 按 region 尺寸存储（regionRows × regionCols）
SMALL_RECT s_lastRegion{0, 0, 0, 0};  // 上次写入的区域
bool s_cacheValid = false;

// 比较 two CHAR_INFO 是否"渲染等价"（字符 + 可见属性相同）
// COMMON_LVB_TRAILING_BYTE 等 LVB 标志不影响渲染输出，但参与缓存比较
// 以确保 trailing cell 的缓存与首次输出一致
inline bool CellEqual(const CHAR_INFO& a, const CHAR_INFO& b) {
    return a.Char.UnicodeChar == b.Char.UnicodeChar
        && a.Attributes == b.Attributes;
}

// 判断 cell 是否为"默认空格"（WT 初始状态）
// 全量输出时跳过这种 cell，减少 VT 字节量
inline bool IsDefaultBlank(const CHAR_INFO& ci) {
    return ci.Char.UnicodeChar == L' ' && ci.Attributes == 0x07;
}
} // namespace

// 失效 diff 缓存：屏幕内容被非 WriteConsoleOutput 路径改变时调用
void ConsoleToVt::InvalidateOutputCache() {
    AcquireSRWLockExclusive(&s_cacheLock);
    s_lastBuffer.clear();
    s_cacheValid = false;
    ReleaseSRWLockExclusive(&s_cacheLock);
}

// ===== Phase 3：WriteConsoleW 翻译 =====
// wchar_t 文本 → UTF-8 + SGR 颜色
std::string ConsoleToVt::WriteConsoleW(const wchar_t* buf, DWORD len, WORD attr) {
    if (buf == nullptr || len == 0) return {};

    std::string out;
    out.reserve(static_cast<size_t>(len) * 3 + 16);

    // 1. 颜色（thread_local 缓存，仅变化时输出 SGR）
    out += vt::SgrFromAttribute(attr);

    // 2. wchar_t → UTF-8
    int utf8Len = WideCharToMultiByte(CP_UTF8, 0, buf, static_cast<int>(len),
                                      nullptr, 0, nullptr, nullptr);
    if (utf8Len > 0) {
        const size_t prev = out.size();
        out.resize(prev + utf8Len);
        WideCharToMultiByte(CP_UTF8, 0, buf, static_cast<int>(len),
                            out.data() + prev, utf8Len, nullptr, nullptr);
    }

    return out;
}

// ===== Phase 4：FillConsoleOutputCharacter =====
// 在 (writeCoord.X, writeCoord.Y) 填充 count 个 character
// VT 策略：光标定位 + 输出一个字符 + CSI (count-1) b 重复
//
// 用途：cls 清屏（character=' '）、绘制水平线等
std::string ConsoleToVt::FillConsoleOutputCharacter(
    wchar_t character, DWORD count, COORD writeCoord) {

    if (count == 0) return {};

    std::string out;
    out.reserve(32);

    // 光标定位（0-based → 1-based）
    out += vt::CursorPosition(writeCoord.Y + 1, writeCoord.X + 1);

    // 输出一个字符（转 UTF-8）
    char utf8[4];
    int len = WideCharToMultiByte(CP_UTF8, 0, &character, 1,
                                  utf8, sizeof(utf8), nullptr, nullptr);
    if (len > 0) {
        out.append(utf8, static_cast<size_t>(len));
    }

    // 重复 count-1 次（CSI N b）
    if (count > 1) {
        out += vt::RepeatChar(static_cast<int>(count - 1));
    }

    return out;
}

// ===== Phase 4：FillConsoleOutputAttribute =====
// 在 (writeCoord.X, writeCoord.Y) 填充 count 个 cell 的颜色属性
// VT 策略：光标定位 + SGR（不输出字符，仅改变颜色状态）
//
// 用途：color 命令改变整屏颜色
// 注意：通常与 FillConsoleOutputCharacter 配对调用（先填属性再填字符），
//       故此处不重复输出字符。count 参数保留用于未来 Phase 10 区域优化。
std::string ConsoleToVt::FillConsoleOutputAttribute(
    WORD attribute, DWORD count, COORD writeCoord) {

    if (count == 0) return {};

    std::string out;
    out.reserve(32);

    // 光标定位（0-based → 1-based）
    out += vt::CursorPosition(writeCoord.Y + 1, writeCoord.X + 1);

    // SGR 颜色（thread_local 缓存，仅变化时输出）
    out += vt::SgrFromAttribute(attribute);

    // 更新 ConsoleState 缓存的当前属性
    ConsoleState::Instance().SetTextAttribute(attribute);

    return out;
}

// ===== Phase 4：WriteConsoleOutput =====
// 写字符矩阵：buffer 是 bufferSize.X * bufferSize.Y 的 CHAR_INFO 数组
// 每个 cell 带字符+属性，需逐 cell 翻译为 光标定位 + SGR + 字符
//
// 优化策略：
//   1. 跳过空格 + 默认属性（0x07）的 cell（避免满屏空格）—— 仅全量路径
//   2. 同行连续相同属性的 cell 合并为一次光标定位 + 多字符
//   3. Phase 10 任务6：diff 路径。若本次 region 与缓存相同，仅输出变化 cell
//
// bufferCoord: 源缓冲区读取起始坐标（通常 {0,0}）
// writeRegion: 目标屏幕区域
std::string ConsoleToVt::WriteConsoleOutput(
    const CHAR_INFO* buffer, COORD bufferSize, COORD bufferCoord,
    SMALL_RECT writeRegion) {

    if (buffer == nullptr || bufferSize.X <= 0 || bufferSize.Y <= 0) return {};

    // 目标区域尺寸
    const int regionRows = writeRegion.Bottom - writeRegion.Top + 1;
    const int regionCols = writeRegion.Right - writeRegion.Left + 1;
    if (regionRows <= 0 || regionCols <= 0) return {};

    // 保存原始光标位置：Windows 真实的 WriteConsoleOutput 不改变光标位置
    // （它直接写屏幕缓冲区，光标保持原位）。
    // 但 VT 翻译过程中输出的 CursorPosition 会移动 ConPTY 光标，
    // 故翻译完成后需发 CursorPosition 把 ConPTY 光标恢复到原位。
    // 同时不更新 ConsoleState 光标缓存（保持原值）。
    COORD savedCursor = ConsoleState::Instance().GetCursorPosition();

    // 提取本次 region 内的 cell 数组（按 regionRows × regionCols 存储）
    // 与 buffer 解耦，便于后续 diff 比较与缓存更新
    std::vector<CHAR_INFO> currentCells(
        static_cast<size_t>(regionRows) * regionCols);
    for (int r = 0; r < regionRows; ++r) {
        int srcRow = bufferCoord.Y + r;
        for (int c = 0; c < regionCols; ++c) {
            int srcCol = bufferCoord.X + c;
            if (srcRow < bufferSize.Y && srcCol < bufferSize.X) {
                currentCells[static_cast<size_t>(r) * regionCols + c] =
                    buffer[srcRow * bufferSize.X + srcCol];
            } else {
                // 源缓冲区越界：用默认空格填充
                CHAR_INFO blank{};
                blank.Char.UnicodeChar = L' ';
                blank.Attributes = 0x07;
                currentCells[static_cast<size_t>(r) * regionCols + c] = blank;
            }
        }
    }

    // 加缓存锁：diff 判断 + 输出 + 缓存更新必须原子
    AcquireSRWLockExclusive(&s_cacheLock);

    const bool canDiff = s_cacheValid
        && s_lastRegion.Left == writeRegion.Left
        && s_lastRegion.Top == writeRegion.Top
        && s_lastRegion.Right == writeRegion.Right
        && s_lastRegion.Bottom == writeRegion.Bottom
        && s_lastBuffer.size() == currentCells.size();

    std::string out;
    // 预估容量：每个 cell 最多 ~20 字节（CSI 8 + SGR 8 + UTF-8 4）
    // diff 路径实际输出远小于此，预留足够容量避免 realloc
    out.reserve(static_cast<size_t>(regionRows) * regionCols * 20);

    WORD lastAttr = 0xFFFF;  // 初始无效值，首次必输出 SGR

    // 遍历目标区域：r/c 是区域内的相对坐标
    // 目标屏幕坐标 = writeRegion.TopLeft + (r, c)
    for (int r = 0; r < regionRows; ++r) {
        bool rowStarted = false;  // 当前行是否已输出光标定位
        WORD rowAttr = lastAttr;

        for (int c = 0; c < regionCols; ++c) {
            const CHAR_INFO& ci = currentCells[static_cast<size_t>(r) * regionCols + c];

            // 优化：跳过双宽字符的 trailing cell（全量与 diff 路径都跳过）
            // Windows Console 中双宽字符（中文/emoji）占 2 个 cell：
            //   leading cell（COMMON_LVB_LEADING_BYTE=0x0100）存字符
            //   trailing cell（COMMON_LVB_TRAILING_BYTE=0x0200）也存同一字符
            // 若 trailing cell 也输出字符，会导致 "版本" 渲染成 "版版本本"
            // 跳过 trailing cell，ConPTY 会自动把 leading 字符渲染为 2 列宽
            if (ci.Attributes & COMMON_LVB_TRAILING_BYTE) {
                continue;
            }

            // 跳过条件：
            //   - 全量路径：跳过默认空格（WT 初始状态，无需输出）
            //   - diff 路径：跳过与缓存相同的 cell（无变化）
            bool skip = false;
            if (canDiff) {
                const CHAR_INFO& last = s_lastBuffer[static_cast<size_t>(r) * regionCols + c];
                if (CellEqual(ci, last)) {
                    skip = true;
                }
            } else {
                if (IsDefaultBlank(ci)) {
                    skip = true;
                }
            }
            if (skip) {
                rowStarted = false;  // 中断连续输出
                continue;
            }

            // 目标屏幕坐标（0-based → 1-based VT）
            int vtRow = writeRegion.Top + r + 1;
            int vtCol = writeRegion.Left + c + 1;

            // 颜色变化时输出 SGR
            if (ci.Attributes != rowAttr) {
                out += vt::SgrFromAttribute(ci.Attributes);
                rowAttr = ci.Attributes;
                lastAttr = rowAttr;
            }

            // 同行连续输出：仅首次需要光标定位
            // 中断后（如跳过 cell）需重新定位
            if (!rowStarted) {
                out += vt::CursorPosition(vtRow, vtCol);
                rowStarted = true;
            }

            // 字符转 UTF-8
            char utf8[4];
            int len = WideCharToMultiByte(CP_UTF8, 0, &ci.Char.UnicodeChar, 1,
                                          utf8, sizeof(utf8), nullptr, nullptr);
            if (len > 0) {
                out.append(utf8, static_cast<size_t>(len));
            }
        }
    }

    // 更新缓存：本次 currentCells 成为下次的 lastBuffer
    // diff 路径下 currentCells 已是最新屏幕状态
    // 全量路径下 currentCells 包含默认空格（与 WT 实际状态一致）
    s_lastBuffer = std::move(currentCells);
    s_lastRegion = writeRegion;
    s_cacheValid = true;

    ReleaseSRWLockExclusive(&s_cacheLock);

    // 翻译完成后恢复 ConPTY 光标到原始位置
    // WriteConsoleOutput 不改变光标位置（Windows 真实行为），但 VT 翻译过程中
    // 输出的 CursorPosition 命令会让 ConPTY 光标移动到最后的 cell 位置。
    // 发 CursorPosition 把 ConPTY 光标拉回 savedCursor，与 ConsoleState 缓存保持一致。
    // 不更新 ConsoleState 缓存（保持原值，符合 Windows 真实行为）。
    // lastAttr 也不更新 ConsoleState（WriteConsoleOutput 不改变当前属性状态）。
    out += vt::CursorPosition(savedCursor.Y + 1, savedCursor.X + 1);

    // Phase 10 任务6：diff 决策日志，便于测试验证 diff 算法生效
    // 格式：WriteConsoleOutput: canDiff=N region=(L,T,R,B) cells=N outBytes=N
    LOG_INFO("WriteConsoleOutput: canDiff=%d region=(%d,%d,%d,%d) cells=%d outBytes=%zu",
             canDiff ? 1 : 0,
             writeRegion.Left, writeRegion.Top,
             writeRegion.Right, writeRegion.Bottom,
             regionRows * regionCols, out.size());

    return out;
}

// ===== Phase 4：WriteConsoleOutputCharacter =====
// 在 (writeCoord.X, writeCoord.Y) 写一串字符（不改颜色）
// VT 策略：光标定位 + UTF-8 字符串
//
// 用途：prompt $P$G 等局部文本输出
std::string ConsoleToVt::WriteConsoleOutputCharacter(
    const wchar_t* buffer, DWORD length, COORD writeCoord) {

    if (buffer == nullptr || length == 0) return {};

    std::string out;
    out.reserve(static_cast<size_t>(length) * 3 + 16);

    // 光标定位（0-based → 1-based）
    out += vt::CursorPosition(writeCoord.Y + 1, writeCoord.X + 1);

    // wchar_t → UTF-8
    int utf8Len = WideCharToMultiByte(CP_UTF8, 0, buffer, static_cast<int>(length),
                                      nullptr, 0, nullptr, nullptr);
    if (utf8Len > 0) {
        const size_t prev = out.size();
        out.resize(prev + utf8Len);
        WideCharToMultiByte(CP_UTF8, 0, buffer, static_cast<int>(length),
                            out.data() + prev, utf8Len, nullptr, nullptr);
    }

    return out;
}

// ===== Phase 4：ScrollConsoleScreenBuffer =====
// 滚动屏幕缓冲区区域
//
// 清屏式滚动检测（cmd.exe 的 cls 实现）：
//   cmd.exe 的 cls 用 ScrollConsoleScreenBufferW 把整个可视区域向上滚动
//   屏幕高度行（dest=(0,-rows)），让内容滚出可视区域实现"清屏"。
//   但 VT 的 ScrollUp (CSI N S) 只让内容进入 WT 滚动缓冲，往上滚仍能看到旧内容，
//   不符合 cls 语义。检测到此模式时改发 EraseDisplay(3)+EraseDisplay(2)+CursorPosition(1,1)
//   清滚动缓冲+清可视区域+光标归位。
//
// 普通滚屏：
//   - 根据 destOrigin.Y - scrollRect.Top 判断上下方向
//   - 上滚用 CSI N S（内容上移，底部空出）
//   - 下滚用 CSI N T（内容下移，顶部空出）
//   - 水平偏移用光标移动（简化处理）
//
// TODO Phase 10：完整实现需处理 clipRect 裁剪与 fillChar 填充，
//                用 DECSTBM (CSI top;bottom r) 设置滚动区域精确控制。
std::string ConsoleToVt::ScrollConsoleScreenBuffer(
    SMALL_RECT scrollRect, const SMALL_RECT* /*clipRect*/,
    COORD destOrigin, wchar_t fillChar, WORD fillAttr) {

    std::string out;

    // 计算垂直偏移
    int dRow = destOrigin.Y - scrollRect.Top;
    int dCol = destOrigin.X - scrollRect.Left;

    // 清屏式滚动检测：目标区域底部（destOrigin.Y + scrollRect.Bottom）滚出屏幕顶部
    // cmd.exe cls 特征：scrollRect.Top==0 且 destOrigin.Y + scrollRect.Bottom <= 0
    //   例如 rect=(0,0,120,30) dest=(0,-30)：-30 + 30 = 0 <= 0，满足
    //
    // VT 策略：
    //   - ScrollUp(rows)：让内容上移出可视区域，ConPTY 同步给 WT 清可视区域
    //   - EraseDisplay(3)：清 WT 滚动缓冲（scrollback），ConPTY 可能转发也可能吞掉
    //   - CursorPosition(1,1)：光标归位
    //   先 ScrollUp 保证可视区域必清（ConPTY 可靠支持），再发 3J 尝试清 scrollback
    if (scrollRect.Top == 0 && destOrigin.Y + scrollRect.Bottom <= 0) {
        int scrollRows = -(destOrigin.Y - scrollRect.Top);  // 上移行数（正数）
        if (scrollRows > 0) {
            out += vt::ScrollUp(scrollRows);  // 清可视区域（ConPTY 可靠支持）
        }
        out += vt::EraseDisplay(3);       // 清 WT 滚动缓冲（scrollback）
        out += vt::CursorPosition(1, 1);  // 光标归位（1-based）
        // fillChar/fillAttr 由 ScrollUp 空出区域默认填充，忽略
        (void)fillChar;
        (void)fillAttr;
        return out;
    }

    // 普通滚屏
    if (dRow > 0) {
        // 内容下移 = 视觉上 ScrollDown
        out += vt::ScrollDown(dRow);
    } else if (dRow < 0) {
        // 内容上移 = 视觉上 ScrollUp
        out += vt::ScrollUp(-dRow);
    }

    if (dCol > 0) {
        out += vt::CursorForward(dCol);
    } else if (dCol < 0) {
        out += vt::CursorBack(-dCol);
    }

    // 简化：忽略 fillChar/fillAttr 填充（Phase 10 补全）
    // 实际场景中 cmd 滚屏 fillChar 通常是空格，WT 滚屏后空出区域默认为空格
    (void)fillChar;
    (void)fillAttr;

    return out;
}

} // namespace terminjector
