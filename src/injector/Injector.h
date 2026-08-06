// DLL 注入器：将 injected.dll 远程加载到目标进程
// 详见 docs/phases/02-injector-modes.md 4.4
//
// 注入流程：
//   1. EnableDebugPrivilege（提升 SeDebugPrivilege）
//   2. OpenProcess（PROCESS_CREATE_THREAD | VM_* | QUERY_INFORMATION）
//   3. 架构校验（IsX64Process，拒绝 32 位目标）
//   4. RemoteLoadLibrary（VirtualAllocEx + WriteProcessMemory + CreateRemoteThread）
//   5. RemoteCallExport 传注入参数（随机管道名 + mediatorPid）
//
// 管道参数传递（安全审查 HIGH #2 修复）：
//   旧方案 DLL 用 GetCurrentProcessId() 自发现约定管道名，名字可预测易被
//   同会话进程预创建抢占。现在管道名由服务端生成随机后缀，注入器经
//   RemoteCallExport 远程调用 DLL 的 RemotePipeSetup 导出函数下发，
//   DLL 连接后校验服务端进程身份 == mediatorPid。
#pragma once

#include <windows.h>
#include <cstdint>
#include <string>

namespace terminjector {

class Injector {
public:
    Injector();
    ~Injector();

    Injector(const Injector&) = delete;
    Injector& operator=(const Injector&) = delete;

    // 注入 DLL 到目标进程
    // targetPid   目标进程 PID
    // dllPath     injected.dll 绝对路径（内部转短路径）
    // pipeName    随机管道名（服务端生成，DLL 用它连接）
    // mediatorPid 管道服务端进程 PID（DLL 连接后校验身份；0=跳过校验）
    // 返回 true 成功
    bool Inject(uint32_t targetPid, const std::wstring& dllPath,
                const std::wstring& pipeName, uint32_t mediatorPid);

private:
    // 提升 SeDebugPrivilege（注入同权限或更低权限进程时非必需，但建议启用）
    bool EnableDebugPrivilege();

    // 远程调用 LoadLibraryW 加载 DLL
    // 返回远程 HMODULE（完整 64 位基址；失败返回 nullptr）
    // 注意：CreateRemoteThread 退出码只有 32 位，不能直接当 HMODULE 用，
    // 内部用 EnumProcessModulesEx 按文件名匹配拿完整基址
    HMODULE RemoteLoadLibrary(HANDLE hProcess, const std::wstring& dllPath);

    // 枚举目标进程模块，按文件名匹配返回模块基址（不区分大小写）
    HMODULE FindRemoteModuleByPath(HANDLE hProcess, const std::wstring& name);
};

} // namespace terminjector
