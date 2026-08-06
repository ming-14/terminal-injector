// 行编辑器：模拟 conhost 在 ENABLE_LINE_INPUT 模式下的行编辑行为
// 详见 docs/phases/06-input-chain.md（行编辑扩展）
//
// 职责：
//   - 维护行缓冲区 + 光标位置
//   - 处理按键事件（字符插入、退格、方向键、Home/End、Delete、Esc）
//   - 生成 VT 输出（回显、重绘）通过 out 参数返回
//   - 命令历史导航（上/下箭头）
//   - Tab 补全（委托给 TabCompleter）
//
// 设计要点：
//   - 单例（历史需跨 ReadConsoleW 调用持久化）
//   - 每次 ReadConsoleW 调用 BeginSession() 重置当前行
//   - VT 输出通过 out 参数返回，调用方负责 SendToMediator
//   - 不处理线程同步（cmd 输入单线程）
//   - 使用相对光标移动（CSI D/C），假设行不跨屏幕换行
//
// ConsoleState 同步：
//   - LineEditor 输出的 VT 序列直接发给 mediator → WT 渲染，
//     DLL 的 ConsoleState 光标缓存不会自动更新
//   - 若不同步，后续 cmd WriteConsoleW 时 OutputHooks 用旧光标定位，
//     会覆盖 LineEditor 已输出的内容（典型现象：输入"你好"+Enter 后
//     "你好"未回显，错误信息直接接在 prompt 后）
//   - 故 BeginSession 记录行首光标 m_startCursor，每次操作后调用
//     SyncCursor 把 ConsoleState 光标同步到 LineEditor 当前状态
#pragma once

#include <windows.h>
#include <string>
#include <vector>
#include <memory>
#include <cstddef>

namespace terminjector {

class TabCompleter;

class LineEditor {
public:
    static LineEditor& Instance();

    // 开始新的行编辑会话（每次 ReadConsoleW 调用时调用）
    void BeginSession();

    // 处理一个按键事件
    // ker: KEY_EVENT_RECORD（调用方应仅传 bKeyDown=TRUE 的事件）
    // echoEnabled: 是否回显（ENABLE_ECHO_INPUT）
    // lineOut: 行完成时输出行内容（不含 \r\n）
    // vtOut: 输出 VT 序列（回显/重绘/Enter 换行）
    // 返回 true: 行完成（Enter 按下）
    // 返回 false: 继续编辑
    bool ProcessKey(const KEY_EVENT_RECORD& ker, bool echoEnabled,
                    std::wstring& lineOut, std::string& vtOut);

    // 当前行内容（诊断用）
    const std::wstring& GetLine() const { return m_line; }
    size_t GetCursor() const { return m_cursor; }

    // 当前期望光标（UI 坐标，0-based）
    // 行首 m_startCursor.X + 光标前显示宽度，超出屏幕宽时折行（供 Phase 21
    // 子进程行编辑回显前补发 CursorPosition 用：父 cmd 启动回显会偏移共享
    // ConPTY 光标，行编辑相对定位从错位位置开始，回车后光标多一行）
    COORD GetCurrentUiCursor() const;

private:
    LineEditor();

    // ---- 行状态 ----
    std::wstring m_line;   // 当前行内容
    size_t m_cursor;       // 光标位置（0 ~ m_line.size()）

    // ---- 代理对缓存 ----
    // ReadConsoleInputW 把 BMP 外字符（如 emoji）拆成高代理+低代理两个 KEY_EVENT，
    // LineEditor 需缓存高代理，等低代理到来后组合为完整字符插入和回显
    bool m_hasHighSurrogate = false;
    wchar_t m_highSurrogate = 0;

    // ---- 行首绝对光标（ConsoleState 同步用）----
    // BeginSession 时从 ConsoleState 读取，作为行首光标基准
    // 操作后新光标 = (m_startCursor.X + 行内显示偏移, m_startCursor.Y + deltaY)
    COORD m_startCursor{0, 0};

    // ---- 历史导航 ----
    std::vector<std::wstring> m_history;  // 历史命令列表
    int m_historyIdx;  // 历史导航索引，-1 表示不在历史导航中
    std::wstring m_savedLine;  // 进入历史导航前保存的当前行

    // ---- Tab 补全 ----
    std::unique_ptr<TabCompleter> m_tabCompleter;

    // ---- 辅助方法 ----

    // 全行重绘：移到行首→输出整行→擦除行末→移回光标
    void FullRedraw(std::string& vtOut) const;

    // wchar_t → UTF-8 转换
    static std::string WToUtf8(const std::wstring& w);

    // 添加到历史（行非空时）
    void AddToHistory(const std::wstring& line);

    // 历史导航：替换当前行为历史条目
    // direction: -1=上一个(Up), +1=下一个(Down)
    void NavigateHistory(int direction, std::string& vtOut);

    // Tab 补全处理
    void HandleTab(bool shift, std::string& vtOut);

    // 替换当前行（历史导航/Tab 补全用）
    void ReplaceLine(const std::wstring& newLine, size_t newCursor,
                     std::string& vtOut);

    // 把 ConsoleState 光标同步到 LineEditor 当前状态
    //   toLineStart=true: X=0（Enter/Ctrl+C 换行后）
    //   toLineStart=false: X = m_startCursor.X + DisplayWidth(m_line, 0, m_cursor)
    //   deltaY: Y 偏移（Enter/Ctrl+C 为 1，其他为 0）
    void SyncCursor(int deltaY = 0, bool toLineStart = false) const;
};

} // namespace terminjector
