// relay32inject.exe —— 32 位引导注入器（Phase 23）
//
// 为什么需要它：
//   x64 injected.dll 在父进程（如 pwsh）里捕获到 32 位子进程（如 C:\Windows\py.exe）
//   时，必须把 relay32.dll 注入进去。但 injected.dll 自己是 64 位，本地
//   kernel32!LoadLibraryW 是 64 位地址，在 32 位目标里没有意义（实测返回 0）。
//   同位数注入才是可行路径，于是由 injected.dll fork 本程序代劳。
//
// 只做一件事：把 relay32.dll 注入到指定进程（可处于 CREATE_SUSPENDED 冻结态），
// 并经 RemotePipeSetup 下发管道参数，然后退出。退出码 0 表示注入成功。
//
// 为什么不让 relay32.dll 兼任注入器：
//   relay32.dll 的 DllMain 会启动"与 mediator 建连"的工作线程。注入器进程里
//   加载它，等于凭空多出一次注定失败的连接等待；而且中继与注入器职责不同，
//   合在一起后"谁在连管道"就说不清了。
//
// 用法（由 ProcessHooks 生成，不面向用户）：
//   relay32inject.exe --child <pid> --dll <relay32.dll 全路径>
//                     --pipe <会话管道名> [--mediator-pid <pid>]
#include <windows.h>

#include <cstdio>
#include <cstdlib>
#include <string>

#include "child_inject.h"
#include "logging/Logger.h"

namespace {

// 取 exe 自身所在目录
std::wstring GetExeDir() {
    wchar_t path[MAX_PATH] = {0};
    if (GetModuleFileNameW(nullptr, path, MAX_PATH) == 0) {
        return L".";
    }
    std::wstring s(path);
    const size_t pos = s.find_last_of(L"\\/");
    return (pos != std::wstring::npos) ? s.substr(0, pos) : L".";
}

// 日志：本程序由 injected.dll 以 CREATE_NO_WINDOW 拉起，没有控制台可看，
// 文件日志是唯一的诊断出口。按目标 pid 分文件，避免并发注入时相互抢句柄。
void InitLogger(uint32_t childPid) {
    const std::wstring logDir = GetExeDir() + L"\\logs";
    CreateDirectoryW(logDir.c_str(), nullptr);
    const std::wstring logPath = logDir + L"\\relay32inject-" +
                                std::to_wstring(childPid) + L".log";
    terminjector::Logger::Initialize(logPath.c_str(), terminjector::LogLevel::Debug);
}

void PrintUsage() {
    std::fprintf(stderr,
                 "usage: relay32inject.exe --child <pid> --dll <path> "
                 "--pipe <name> [--mediator-pid <pid>]\n");
}

} // namespace

int wmain(int argc, wchar_t** argv) {
    uint32_t childPid = 0;
    uint32_t mediatorPid = 0;
    std::wstring dllPath;
    std::wstring pipeName;

    for (int i = 1; i < argc; ++i) {
        const std::wstring a = argv[i];
        const bool hasNext = (i + 1 < argc);
        if (a == L"--child" && hasNext) {
            childPid = static_cast<uint32_t>(wcstoul(argv[++i], nullptr, 10));
        } else if (a == L"--dll" && hasNext) {
            dllPath = argv[++i];
        } else if (a == L"--pipe" && hasNext) {
            pipeName = argv[++i];
        } else if (a == L"--mediator-pid" && hasNext) {
            mediatorPid = static_cast<uint32_t>(wcstoul(argv[++i], nullptr, 10));
        } else {
            std::fprintf(stderr, "unknown argument: %ls\n", a.c_str());
            PrintUsage();
            return 2;
        }
    }

    if (childPid == 0 || dllPath.empty() || pipeName.empty()) {
        PrintUsage();
        return 2;
    }

    InitLogger(childPid);
    LOG_INFO("=== relay32inject start, child=%u dll=%ls pipe=%ls mediatorPid=%u ===",
             childPid, dllPath.c_str(), pipeName.c_str(), mediatorPid);

    // 目标此时被父进程冻在 CREATE_SUSPENDED，因此必须拿到全部注入权限：
    // 远程线程 + 远程内存读写。
    HANDLE hProcess = OpenProcess(
        PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
        PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE,
        FALSE, childPid);
    if (hProcess == nullptr) {
        LOG_ERROR("OpenProcess(%u) failed: %lu", childPid, GetLastError());
        terminjector::Logger::Shutdown();
        return 1;
    }

    const bool ok = terminjector::relay32::InjectDllSameBitness(
        hProcess, childPid, dllPath, pipeName, mediatorPid);
    CloseHandle(hProcess);

    LOG_INFO("=== relay32inject exit, child=%u ok=%d ===", childPid, ok ? 1 : 0);
    terminjector::Logger::Shutdown();
    return ok ? 0 : 1;
}
