// 进程辅助函数
// 详见 docs/phases/02-injector-modes.md 4.4.3
//
// 提供：
//   - IsX64Process：判断目标进程架构（仅支持注入 x64 进程）
//   - ToShortPath：长路径转短路径（避免远程写入时空格引发解析问题）
//   - FindProcessByName：按进程名查 PID（可选，用于交互式选择目标）
//   - FindRemoteModuleByPath：按文件名匹配目标进程模块基址（原 Injector 成员，
//     Phase 19 提升为公共工具供 --list-targets 复用）
//   - EnumerateInjectTargets：枚举可注入进程（--list-targets 数据源）
#pragma once

#include <windows.h>
#include <cstdint>
#include <string>
#include <vector>

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

// 枚举目标进程模块，按文件名（不区分大小写）匹配返回模块基址；未找到返回 nullptr
// hProcess 需 PROCESS_QUERY_INFORMATION | PROCESS_VM_READ（EnumProcessModulesEx）
HMODULE FindRemoteModuleByPath(HANDLE hProcess, const std::wstring& name);

// 单个进程的可注入性判定结果（--list-targets 条目）
struct InjectTargetInfo {
    uint32_t    pid = 0;             // 进程 PID
    std::wstring name;               // exe 文件名（如 cmd.exe）
    bool        x64 = false;         // 是否 x64 架构
    bool        cui = false;         // PE Subsystem 是否为 CUI（控制台程序）
    bool        injectable = false;  // 权限 + x64 + CUI 全部满足
    bool        alreadyInjected = false; // 已加载 injected.dll（仍可注入，重复无意义）
    std::wstring reason;             // 不可注入原因（可注入时为空串）
    std::wstring startTime;          // 启动时间（本地时间，如 2026-08-08 12:30:45）
    std::wstring cmdLine;            // 启动命令行（可能较长，GUI 折行显示）
};

// 枚举全部进程并判定可注入性（--list-targets 数据源）
// 判定规则（与 Injector::Inject 前提一致 + 控制台类型过滤）：
//   1. 排除自身进程与系统进程（PID 0/4）
//   2. OpenProcess（注入所需全套权限）失败 → reason=access_denied
//   3. IsX64Process 非 x64 → reason=not_x64
//   4. PE Subsystem 非 CUI（GUI/未知）→ reason=not_console
//   5. 全部满足 → injectable；再查是否已加载 injected.dll
// 返回值按 PID 升序
std::vector<InjectTargetInfo> EnumerateInjectTargets();

// 提升 SeDebugPrivilege（管理员运行时能打开更多进程）
// 返回 true 表示已获得；失败（非管理员）返回 false，同权限目标不受影响
bool EnableDebugPrivilege();

} // namespace terminjector::ProcessHelper
