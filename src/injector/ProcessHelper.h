// 进程辅助函数
// 详见 docs/phases/02-injector-modes.md 4.4.3
//
// 提供：
//   - IsX64Process：判断目标进程架构（仅支持注入 x64 进程）
//   - ToShortPath：长路径转短路径（避免远程写入时空格引发解析问题）
//   - FindProcessByName：按进程名查 PID（可选，用于交互式选择目标）
#pragma once

#include <windows.h>
#include <cstdint>
#include <string>

namespace terminjector::ProcessHelper {

// 判断目标进程是否 x64 架构
// hProcess 已打开的进程句柄（需 PROCESS_QUERY_INFORMATION）
// 返回 true 表示是 x64 进程；false 表示是 WoW64（32 位）进程或查询失败
bool IsX64Process(HANDLE hProcess);

// 长路径转 8.3 短路径
// longPath 输入路径（可能含空格/中文）
// outShort 输出短路径
// 返回 true 成功
bool ToShortPath(const std::wstring& longPath, std::wstring& outShort);

// 按进程名查找 PID（第一个匹配）
// name 进程名（如 L"cmd.exe"），不区分大小写
// 返回 PID；未找到返回 0
uint32_t FindProcessByName(const std::wstring& name);

} // namespace terminjector::ProcessHelper
