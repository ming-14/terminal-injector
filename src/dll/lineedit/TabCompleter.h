// Tab 补全器：枚举文件系统匹配并循环补全
// 详见 docs/phases/06-input-chain.md（行编辑扩展）
//
// 职责：
//   - 解析当前行中要补全的 word（空格分隔）
//   - 枚举匹配的文件/目录（FindFirstFileW/FindNextFileW）
//   - 循环补全：首次 Tab 返回第一个匹配，再次 Tab 循环下一个
//
// 设计要点：
//   - 无持久状态（每次 Complete 调用重新枚举）
//   - 补全后自动给目录追加 '\'，与 conhost 行为一致
//   - 支持相对路径和绝对路径
#pragma once

#include <string>
#include <vector>
#include <cstddef>

namespace terminjector {

class TabCompleter {
public:
    TabCompleter();

    // 开始补全（首次按 Tab）
    // line: 当前行内容
    // cursor: 光标位置
    // outLine: 输出补全后的完整行
    // outCursor: 输出补全后的光标位置
    // 返回 true: 有匹配，outLine/outCursor 有效
    // 返回 false: 无匹配
    bool Complete(const std::wstring& line, size_t cursor,
                  std::wstring& outLine, size_t& outCursor);

    // 循环下一个匹配（再次按 Tab）
    // 返回 true: 有下一个匹配
    // 返回 false: 已循环一圈回到第一个
    bool Next(std::wstring& outLine, size_t& outCursor);

    // 反向循环（Shift+Tab）
    bool Prev(std::wstring& outLine, size_t& outCursor);

    // 取消补全模式（按了非 Tab 键后调用）
    void Cancel() { m_active = false; }
    bool IsActive() const { return m_active; }

private:
    // 解析当前行，找到要补全的 word
    // wordStart: word 起始位置（输出）
    // wordEnd: word 结束位置（= cursor，输出）
    // word: word 内容（输出）
    void FindWord(const std::wstring& line, size_t cursor,
                  size_t& wordStart, size_t& wordEnd,
                  std::wstring& word) const;

    // 枚举匹配的文件/目录
    // prefix: 要匹配的前缀（可能含路径）
    // 返回匹配列表（仅文件名，不含路径）
    std::vector<std::wstring> EnumerateMatches(const std::wstring& prefix) const;

    // 应用匹配到行，生成补全后的完整行
    void ApplyMatch(const std::wstring& match,
                    std::wstring& outLine, size_t& outCursor) const;

    // ---- 补全状态 ----
    bool m_active;             // 是否在补全模式
    size_t m_wordStart;        // word 起始位置
    size_t m_wordEnd;          // word 结束位置
    std::wstring m_lineBefore; // word 之前的行内容
    std::wstring m_lineAfter;  // word 之后的行内容
    std::wstring m_pathPrefix; // 路径前缀（如 "subdir\" 或空）
    std::vector<std::wstring> m_matches; // 匹配列表
    size_t m_matchIdx;         // 当前匹配索引
};

} // namespace terminjector
