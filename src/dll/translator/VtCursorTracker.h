// VtCursorTracker:VT 输出直通虚拟光标跟踪(Phase 19,全面版)
//
// 背景:
//   目标进程启用 ENABLE_VIRTUAL_TERMINAL_PROCESSING(如 python/vim/less)后,
//   WriteFile/WriteConsoleW 输出走 VT 直通分支(OutputHooks.cpp),字节原样转发
//   mediator,不推进 VirtualConsoleState。程序随后 GetConsoleScreenBufferInfo
//   查询到陈旧光标,导致"写后查询光标偏差"测试失败(POS_AFTER_WRITE / CURSOR_5
//   / width 系列)。
//
// 本跟踪器解析直通的 VT 字节流,维护与 ConPTY 一致的语义光标:
//   - 文本(UTF-8 / wchar):按 wcwidth 宽度推进,带 wrap pending / DECAWM 语义
//   - CSI 光标指令:CUP/HVP/CHA/VPA(绝对)、CUU/CUD/CUF/CUB/CNL/CPL(相对)、
//     CHT/CBT(tab)、SU/SD/IL/DL/ICH/DCH/ECH(滚动/插入删除,光标不动)
//   - DECSTBM 滚动区:Linefeed/IND/RI/NEL 按区域滚动
//   - DECSC/DECRC(ESC 7/8、CSI s/u):保存/恢复光标
//   - 备用屏:DECSET 47/1047/1048/1049,保存/恢复光标
//   - DECAWM(CSI ?7 h/l)、ED3 清滚存(RIS 复位)
//   - EL/ED/SGR/DSR/DA/其余 DECSET:识别并忽略,不影响光标
//
// 语义取舍(文档 docs/phases/19-vt-cursor-tracker.md):
//   - 相对行移动(CUU/CUD/CNL/CPL)只 clamp 不滚动;滚动语义由 \n/IND/RI/NEL
//     SU/SD 承担,与常见 TUI 行为一致
//   - 备用屏只跟踪光标基准(1049 进入/退出保存/恢复),不跟踪屏幕内容
//   - 全屏滚动计入 VirtualConsoleState::NotifyScrollLine,与 Phase 18 一致
//
// 线程安全:直通输出可能来自多线程,Feed 内部加锁原子解析。
#pragma once

#include <windows.h>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

namespace terminjector {

class VtCursorTracker {
public:
    static VtCursorTracker& Instance();

    // 喂入直通的 VT 字节流(WriteFile_Detour 的原始字节,UTF-8/ANSI)
    void Feed(const char* data, size_t len);
    // 喂入 wchar 直通流(WriteConsoleW_Detour,内部转 UTF-8 后走同一条解析链)
    void Feed(const wchar_t* data, size_t len);

    // 当前跟踪光标(语义 ConPTY 光标,1 为基准在调用方转 1-based)。
    // OutputHooks 在直通写前用它补发 CursorPosition,强制 ConPTY 真实光标
    // 与本跟踪器基准一致,消除父进程输出对共享 ConPTY 光标的影响
    // (Phase 21: width 系列 / long_line_enter 失败根因)。
    COORD GetCursorPosition() const;

private:
    enum class State : uint8_t {
        Ground,       // 普通文本
        Esc,          // 收到 ESC,待定后续
        Csi,          // CSI 参数/中间/最终字节收集
        Osc,          // OSC 直到 BEL/ST
        Dcs,          // DCS/APC/PM 直到 ST
        CharsetSel,   // ESC ( ) * + 后接一个字符集字节
    };

    VtCursorTracker() = default;
    VtCursorTracker(const VtCursorTracker&) = delete;
    VtCursorTracker& operator=(const VtCursorTracker&) = delete;

    // ---- 字节级解析 ----
    void ProcessByte(unsigned char b);
    void FlushText();                      // 暂存 codepoint 逐个推进光标
    void ResetCsi();

    // ---- 语义操作(光标推进) ----
    void LoadDimensions();                 // 从 VirtualConsoleState 同步 cols/rows
    void SyncFromVcs();                    // 从 vcs 拉一次光标基准(Feed 入口)
    void CommitCursor();                   // m_pos 写回 vcs
    void PutCodepoint(uint32_t cp);        // 单个可打印/控制 codepoint
    void Linefeed();                       // Y+1;区域/全屏底部时滚动
    void ReverseIndex();                   // RI(ESC M)
    void ScrollUpInRegion(int n);          // SU/Linefeed 滚动,光标不动
    void ScrollDownInRegion(int n);        // SD/RI 滚动,光标不动
    void NotifyScrollOut();                // 全屏滚动出屏计数
    void ClampCursor();
    void SaveCursor();                     // DECSC
    void RestoreCursor();                  // DECRC
    void EnterAltScreen(bool restore);     // 1047/47/1049 进入
    void ExitAltScreen(bool restore);      // 退出
    void ResetState();                     // RIS

    void DispatchCsi();                    // 普通 CSI final
    void DispatchDecSet(bool enable);      // CSI ? Ps h/l
    int CsiParam(size_t i, int dft) const;

    // ---- 内部状态 ----
    State m_state = State::Ground;

    // 光标核心状态(与 ConPTY 语义一致)
    COORD m_pos{0, 0};
    bool m_wrapPending = false;            // 末列待换行(wrap pending)
    bool m_autoWrap = true;                // DECAWM
    bool m_regionSet = false;              // DECSTBM
    int m_regionTop = 0;                   // 0-based inclusive
    int m_regionBottom = 0;                // 0-based inclusive

    // DECSC 保存
    bool m_savedValid = false;
    COORD m_savedPos{0, 0};
    bool m_savedWrap = false;

    // 备用屏
    bool m_altActive = false;
    COORD m_altSavedPos{0, 0};

    // 尺寸缓存(Feed 入口 LoadDimensions 刷新)
    int m_cols = 80;
    int m_rows = 25;

    // CSI 收集
    std::vector<int> m_params;             // 已解析参数(缺失时按缺省处理)
    bool m_hasParams = false;
    int m_curParamVal = 0;
    bool m_question = false;               // '?' 私有标记(DECSET/DECRST)
    std::string m_inter;                   // 中间字节 0x20-0x2F
    unsigned char m_final = 0;             // 最终字节 0x40-0x7E

    // OSC/DCS 终止检测
    bool m_escSeen = false;                // 刚收到 ESC,等待 ST 的 '\'

    // 文本暂存(codepoint,遇序列中断 flush)
    std::vector<uint32_t> m_textBuf;
    // 多字节 UTF-8 累积
    std::string m_utf8Carry;
    int m_utf8Need = 0;

    // 锁跨非 Feed 只读接口(GetCursorPosition)使用,声明 mutable
    mutable std::mutex m_lock;
};

} // namespace terminjector
