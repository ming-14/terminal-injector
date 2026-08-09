// TabCompleter 实现：文件系统枚举与循环补全
// 详见 docs/phases/06-input-chain.md（行编辑扩展）
//
// 补全策略：
//   1. FindWord：从光标位置向前找空格，确定 word 边界
//   2. EnumerateMatches：
//      - 分离路径前缀（dir\file → dir\ 前缀 + file 匹配）
//      - 在对应目录用 FindFirstFileW 枚举
//      - 匹配文件名前缀（不区分大小写）
//   3. ApplyMatch：替换 word 为匹配项，目录追加 '\'
//   4. 循环：Next/Prev 在匹配列表中循环
#include "TabCompleter.h"
#include "logging/Logger.h"

#include <windows.h>
#include <algorithm>
#include <cstring>

namespace terminjector {

TabCompleter::TabCompleter()
    : m_active(false)
    , m_wordStart(0)
    , m_wordEnd(0)
    , m_matchIdx(0) {
}

// ============================================================
// 解析 word 边界
// ============================================================
// 从光标位置向前找空格，确定 word 起始位置
// word 结束位置 = cursor（补全光标处的 word）
void TabCompleter::FindWord(const std::wstring& line, size_t cursor,
                             size_t& wordStart, size_t& wordEnd,
                             std::wstring& word) const {
    wordEnd = cursor;
    wordStart = cursor;

    // 向前找空格或行首
    while (wordStart > 0) {
        wchar_t c = line[wordStart - 1];
        if (c == L' ' || c == L'\t') break;
        --wordStart;
    }

    word = line.substr(wordStart, wordEnd - wordStart);
}

// ============================================================
// 枚举匹配的文件/目录
// ============================================================
// prefix 可能含路径分隔符（如 "sub\file" 或 "<盘符>:\<目录>\<文件>" 形式的绝对路径）
// 分离路径前缀和文件名前缀，在对应目录枚举
std::vector<std::wstring> TabCompleter::EnumerateMatches(
    const std::wstring& prefix) const {

    std::vector<std::wstring> matches;

    // 分离路径前缀和文件名前缀
    // 找最后一个 \ 或 /
    size_t lastSep = std::wstring::npos;
    for (size_t i = prefix.size(); i > 0; --i) {
        wchar_t c = prefix[i - 1];
        if (c == L'\\' || c == L'/') {
            lastSep = i - 1;
            break;
        }
    }

    std::wstring dirPath;
    std::wstring filePrefix;
    if (lastSep != std::wstring::npos) {
        dirPath = prefix.substr(0, lastSep + 1);
        filePrefix = prefix.substr(lastSep + 1);
    } else {
        dirPath = L".\\";
        filePrefix = prefix;
    }

    // 构造搜索模式：dirPath\*
    std::wstring searchPattern = dirPath + L"*";

    // 枚举目录
    WIN32_FIND_DATAW findData;
    HANDLE hFind = FindFirstFileW(searchPattern.c_str(), &findData);
    if (hFind == INVALID_HANDLE_VALUE) {
        LOG_DEBUG("TabCompleter: FindFirstFile failed, pattern=%ls err=%lu",
                  searchPattern.c_str(), GetLastError());
        return matches;
    }

    // 转换 filePrefix 为小写用于不区分大小写比较
    std::wstring filePrefixLower = filePrefix;
    std::transform(filePrefixLower.begin(), filePrefixLower.end(),
                   filePrefixLower.begin(), ::towlower);

    do {
        // 跳过 "." 和 ".."
        if (wcscmp(findData.cFileName, L".") == 0 ||
            wcscmp(findData.cFileName, L"..") == 0) {
            continue;
        }

        // 匹配前缀（不区分大小写）
        std::wstring nameLower = findData.cFileName;
        // 仅比较前 filePrefixLower.length() 个字符
        if (nameLower.size() < filePrefixLower.size()) continue;
        std::transform(nameLower.begin(), nameLower.end(),
                       nameLower.begin(), ::towlower);
        if (nameLower.compare(0, filePrefixLower.size(), filePrefixLower) != 0) {
            continue;
        }

        // 匹配成功：目录追加 '\'，文件不追加
        std::wstring matchName = findData.cFileName;
        if (findData.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) {
            matchName += L'\\';
        }
        matches.push_back(matchName);

    } while (FindNextFileW(hFind, &findData));

    FindClose(hFind);

    // 排序（字母序，与 conhost 一致）
    std::sort(matches.begin(), matches.end(),
              [](const std::wstring& a, const std::wstring& b) {
                  return ::_wcsicmp(a.c_str(), b.c_str()) < 0;
              });

    LOG_DEBUG("TabCompleter: prefix=%ls dir=%ls matches=%zu",
              filePrefix.c_str(), dirPath.c_str(), matches.size());
    return matches;
}

// ============================================================
// 应用匹配到行
// ============================================================
void TabCompleter::ApplyMatch(const std::wstring& match,
                               std::wstring& outLine,
                               size_t& outCursor) const {
    // 拼接：lineBefore + pathPrefix + match + lineAfter
    outLine = m_lineBefore + m_pathPrefix + match + m_lineAfter;
    // 光标放在补全后 word 的末尾
    outCursor = m_lineBefore.size() + m_pathPrefix.size() + match.size();
}

// ============================================================
// 开始补全
// ============================================================
bool TabCompleter::Complete(const std::wstring& line, size_t cursor,
                             std::wstring& outLine, size_t& outCursor) {
    // 解析 word
    std::wstring word;
    FindWord(line, cursor, m_wordStart, m_wordEnd, word);

    // 保存行上下文
    m_lineBefore = line.substr(0, m_wordStart);
    m_lineAfter = line.substr(m_wordEnd);

    // 分离路径前缀
    size_t lastSep = std::wstring::npos;
    for (size_t i = word.size(); i > 0; --i) {
        wchar_t c = word[i - 1];
        if (c == L'\\' || c == L'/') {
            lastSep = i - 1;
            break;
        }
    }
    if (lastSep != std::wstring::npos) {
        m_pathPrefix = word.substr(0, lastSep + 1);
    } else {
        m_pathPrefix.clear();
    }

    // 枚举匹配
    m_matches = EnumerateMatches(word);
    if (m_matches.empty()) {
        m_active = false;
        return false;
    }

    // 应用第一个匹配
    m_matchIdx = 0;
    m_active = true;
    ApplyMatch(m_matches[m_matchIdx], outLine, outCursor);
    return true;
}

// ============================================================
// 循环下一个匹配
// ============================================================
bool TabCompleter::Next(std::wstring& outLine, size_t& outCursor) {
    if (!m_active || m_matches.empty()) return false;

    m_matchIdx = (m_matchIdx + 1) % m_matches.size();
    ApplyMatch(m_matches[m_matchIdx], outLine, outCursor);
    // 返回 false 表示已循环一圈
    return m_matchIdx != 0;
}

// ============================================================
// 反向循环
// ============================================================
bool TabCompleter::Prev(std::wstring& outLine, size_t& outCursor) {
    if (!m_active || m_matches.empty()) return false;

    if (m_matchIdx == 0) {
        m_matchIdx = m_matches.size() - 1;
        ApplyMatch(m_matches[m_matchIdx], outLine, outCursor);
        return false; // 已循环一圈
    } else {
        --m_matchIdx;
        ApplyMatch(m_matches[m_matchIdx], outLine, outCursor);
        return true;
    }
}

} // namespace terminjector
