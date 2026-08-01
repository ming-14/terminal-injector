// 运行期 Console 状态缓存
// 详见 docs/phases/03-dll-framework.md 4.4
//
// 设计要点：
//   - 单例（Instance），进程级唯一
//   - Hook 安装后，所有 Get* 类 API 返回这里缓存的值
//   - 所有 Set* 类 API 与 Write* 类 API 更新这里
//   - SRWLOCK 保护，读写均线程安全
//   - InitFromSnapshot 在懒加载时用快照初始化
//
// AdvanceCursor 是关键：WriteConsole Hook 拦截输出后必须更新光标缓存，
// 否则下次 GetConsoleScreenBufferInfo 返回旧坐标，目标程序状态不一致
#pragma once

#include <windows.h>
#include <atomic>
#include <mutex>
#include <string>
#include "StateSnapshot.h"

namespace terminjector {

class ConsoleState {
public:
    static ConsoleState& Instance();

    // 用快照初始化（懒加载时调用一次）
    void InitFromSnapshot(const StateSnapshot& snap);

    // ---- 屏幕缓冲区尺寸 ----
    COORD GetBufferSize() const;
    void  SetBufferSize(COORD size);

    // ---- 窗口位置/尺寸（srWindow） ----
    SMALL_RECT GetWindow() const;
    void  SetWindow(SMALL_RECT w);

    // ---- 光标位置 ----
    COORD GetCursorPosition() const;
    void  SetCursorPosition(COORD pos);
    // 输出后自动推进光标（WriteConsole Hook 调用）
    // 解析 buf 中的控制字符（\r \n \b \t）按 ConHost 语义更新光标：
    //   \r → X=0，\n → Y++（不滚屏时钳制），\b → X--，\t → 下一 tab stop
    //   可见字符 → X++，行末按 wrapAtEol 决定 wrap 或停留
    // wrapAtEol=true 时行末换行（正常输出），false 时停在行末
    // 修复 Phase 6 换行异常：旧实现只按 charsWritten 盲目累加 X，
    // 把 \r \n 当可见字符导致 cursor 漂移
    void  AdvanceCursor(const wchar_t* buf, int len, bool wrapAtEol);

    // ---- Phase 5：一次填充全部屏幕缓冲区信息 ----
    // 用于 GetConsoleScreenBufferInfo Hook：返回缓存，不调原 API
    // info 的 dwMaximumWindowSize 用当前 srWindow 大小近似
    void FillScreenBufferInfo(CONSOLE_SCREEN_BUFFER_INFO& info) const;

    // ---- 光标显隐/大小 ----
    CONSOLE_CURSOR_INFO GetCursorInfo() const;
    void SetCursorInfo(const CONSOLE_CURSOR_INFO& info);

    // ---- 模式 ----
    DWORD GetInputMode() const;
    void  SetInputMode(DWORD m);
    DWORD GetOutputMode() const;
    void  SetOutputMode(DWORD m);

    // ---- 代码页 ----
    UINT GetInputCp() const;
    void SetInputCp(UINT cp);
    UINT GetOutputCp() const;
    void SetOutputCp(UINT cp);

    // ---- 当前颜色属性（WriteConsole 时用于 VT 颜色生成） ----
    WORD  GetTextAttribute() const;
    void  SetTextAttribute(WORD attr);

    // ---- 标题 ----
    std::wstring GetTitle() const;
    void SetTitle(const std::wstring& t);

    // ---- Alt Buffer 状态（Phase 8） ----
    bool IsAltBufferActive() const;
    void SetAltBufferActive(bool b);

    // ---- Alt Buffer 句柄（Phase 8） ----
    // 主缓冲区句柄 = GetStdHandle(STD_OUTPUT_HANDLE)，在 InitFromSnapshot 缓存
    // Alt 缓冲区句柄 = CreateConsoleScreenBuffer Hook 返回的伪句柄
    // SetConsoleActiveScreenBuffer Hook 据此判断切换方向：
    //   传入句柄 == 主 → 退出 Alt；传入句柄 == Alt → 进入 Alt
    HANDLE GetMainBufferHandle() const;
    HANDLE GetAltBufferHandle() const;
    void   SetAltBufferHandle(HANDLE h);

    // ---- 字体信息（Phase 8） ----
    // WT 字体由用户配置控制，DLL 仅缓存注入瞬间的字体让 GetCurrentConsoleFontEx
    // 返回稳定值，避免目标程序因字体尺寸不一致导致布局错乱
    CONSOLE_FONT_INFOEX GetFontInfo() const;
    void SetFontInfo(const CONSOLE_FONT_INFOEX& info);

    // ---- 鼠标按键状态（Phase 16） ----
    // 跟踪跨 VtToInputRecord::ParseMouse 调用的鼠标按键状态，
    // 用于 SGR 1006 → INPUT_RECORD 转换时维护 dwButtonState 连续性
    // 替代静态变量 s_buttonState，消除多会话/模式切换时的状态污染
    DWORD GetMouseButtonState() const;
    void SetMouseButtonState(DWORD state);

private:
    ConsoleState() = default;

    // SRWLOCK 比 mutex 轻量，兼容 kernel32 原生（不依赖 CRT）
    mutable SRWLOCK m_lock = SRWLOCK_INIT;

    // 状态字段（受 m_lock 保护）
    CONSOLE_SCREEN_BUFFER_INFO m_screenInfo{};  // 含 dwSize / srWindow / dwCursorPosition / wAttributes
    CONSOLE_CURSOR_INFO        m_cursorInfo{};
    DWORD m_inputMode = 0;
    DWORD m_outputMode = 0;
    UINT  m_inputCp = 0;
    UINT  m_outputCp = 0;
    std::wstring m_title;
    std::atomic<bool> m_altBuffer{false};
    HANDLE m_mainBufferHandle = nullptr;  // GetStdHandle(STD_OUTPUT_HANDLE) 缓存
    HANDLE m_altBufferHandle  = nullptr;  // CreateConsoleScreenBuffer 伪句柄
    CONSOLE_FONT_INFOEX m_fontInfo{};     // 注入瞬间快照，GetCurrentConsoleFontEx 返回此值
    DWORD m_mouseButtonState = 0;         // Phase 16：鼠标按键状态（跨 ParseMouse 调用）

    // ---- 滚动缓冲区（Phase 18） ----
    // 同步跟踪 VirtualConsoleState 的滚动计数
    int32_t m_scrollbackLines = 0;
    int32_t GetScrollbackLines() const;
    void SetScrollbackLines(int32_t n);
};

} // namespace terminjector
