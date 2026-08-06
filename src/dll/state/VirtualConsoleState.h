// 虚拟 Console 状态（Phase 14）
// 详见 docs/phases/14-virtual-console-state.md
//
// 设计要点：
//   - DLL 内唯一权威的 Console 状态，所有 Set* Hook 写入时更新，
//     所有 Get* Hook 查询时返回，保证程序查询到的状态与 WT 一致
//   - 单例（Meyers's Singleton），std::mutex 保护
//   - 支持 WT 反向同步：resize 更新 bufferSize/srWindow，DSR CPR 更新 cursorPos
//   - ConHost 跟随：调用 SyncToConHost 让 ConHost 状态同步到虚拟状态
//
// 关键原则：
//   - WT 是物理终端，是状态的唯一真实源
//   - ConHost 跟随虚拟状态变化，仅作为程序可见的镜像
#pragma once

#include <windows.h>
#include <atomic>
#include <mutex>

namespace terminjector {

class VirtualConsoleState {
public:
    static VirtualConsoleState& Instance();

    // === 初始化 ===
    // 从 ConHost 加载初始状态（LazyInit 时调用一次）
    void InitializeFromConHost();

    // === 光标位置（0-based cell 坐标） ===
    COORD GetCursorPos() const;
    void SetCursorPos(COORD pos);
    void AdvanceCursor(const wchar_t* buf, int len);

    // === 缓冲区大小 ===
    COORD GetBufferSize() const;
    void SetBufferSize(COORD size);

    // === 可见窗口区域（srWindow） ===
    SMALL_RECT GetWindowRect() const;
    void SetWindowRect(SMALL_RECT rect);

    // === 文本属性 ===
    WORD GetAttributes() const;
    void SetAttributes(WORD attr);

    // === 最大窗口大小 ===
    COORD GetLargestWindowSize() const;

    // === 填充 CONSOLE_SCREEN_BUFFER_INFO ===
    void FillScreenBufferInfo(CONSOLE_SCREEN_BUFFER_INFO& info) const;

    // === WT 反向同步接口 ===
    void ApplyWtResize(int32_t cols, int32_t rows);
    void ApplyWtCursorReport(int32_t col, int32_t row);
    // Phase 15：应用 DA 报告（终端能力标识）
    void ApplyWtDaReport(int32_t caps);

    // === 终端能力查询 ===
    int32_t GetTerminalCaps() const { return m_terminalCaps.load(); }

    // === 是否已初始化 ===
    bool IsInitialized() const { return m_initialized.load(); }

    // ---- 滚动缓冲区（Phase 18） ----
    // 获取当前滚动计数（内容滚出视口顶部的行数）
    int32_t GetScrollbackLines() const;
    // 设置用户请求的缓冲区高度（SetConsoleScreenBufferSize 调用）
    // 当 WT resize 时，以此值为下限保留缓冲区高度
    void SetUserBufferHeight(int32_t height);
    // 模式切换时重置滚动计数
    void ResetScrollback();
    // Phase 19：VT 直通追踪器滚动计数（内容滚出视口顶部一行）
    void NotifyScrollLine();

private:
    VirtualConsoleState() = default;

    mutable std::mutex m_lock;
    COORD m_cursorPos{0, 0};
    COORD m_bufferSize{80, 25};
    SMALL_RECT m_windowRect{0, 0, 79, 24};
    std::atomic<WORD> m_attributes{FOREGROUND_BLUE | FOREGROUND_GREEN | FOREGROUND_RED};
    std::atomic<int32_t> m_terminalCaps{0};  // Phase 15：终端能力标识（DA 响应）
    std::atomic<bool> m_initialized{false};

    // ---- 滚动缓冲区（Phase 18） ----
    int32_t m_scrollbackLines = 0;       // 内容滚出视口顶部的行数
    int32_t m_userBufferHeight = 0;      // 用户通过 SetConsoleScreenBufferSize 请求的缓冲区高度
};

} // namespace terminjector