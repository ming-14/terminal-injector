// terminal-injector 双模式入口
// 详见 docs/phases/02-injector-modes.md 4.2 / 4.3
//
// 用法：
//   terminal-injector.exe --inject <pid> [--dll <path>]
//       注入模式：将 injected.dll 注入到 <pid> 进程
//   terminal-injector.exe --mediator --target-pid <pid> [--pipe <name>] [--dll <path>]
//       中介模式：作为 WT 子进程，建立管道等待 DLL 连接
//   terminal-injector.exe --unload-remote <pid> <dllBase>
//       远程卸载助手（Phase 11）：在目标进程创建远程线程调 FreeLibrary(dllBase)
//       由 injected.dll 的 Unloader 在 DoUnload 末尾启动，独立于 WT 生命周期
//   terminal-injector.exe --version
//   terminal-injector.exe --help
//
// 参数解析手写（不引入第三方 CLI 库），保持依赖最小
#include "logging/Logger.h"
#include "transport/NamedPipeTransport.h"  // MakeRandomPipeName
#include "injector/Injector.h"
#include "injector/ProcessHelper.h"  // EnumerateInjectTargets / EnableDebugPrivilege
#include "mediator/Mediator.h"

#include <algorithm>  // std::sort（--list-targets）
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <windows.h>
#include <tlhelp32.h>  // Phase 11：CreateToolhelp32Snapshot 检查模块是否加载

namespace terminjector {

// 命令行参数
struct CliArgs {
    enum class Mode { None, Inject, Mediator, UnloadRemote, ListTargets, Help, Version };
    Mode         mode = Mode::None;
    uint32_t     targetPid = 0;
    uint64_t     dllBase = 0;   // --unload-remote 模式：injected.dll 基址
    std::wstring dllPath;    // 默认 exe 同目录的 injected.dll
    std::wstring pipeName;   // 空则按模式生成随机名（防可预测抢占）
    uint32_t     mediatorPid = 0;  // --mediator-pid：DLL 服务端身份校验目标
    bool         json = false;     // --list-targets 输出 JSON 格式

    bool valid = false;
};

// 获取 exe 自身所在目录
static std::wstring GetExeDir() {
    wchar_t exePath[MAX_PATH] = {0};
    if (GetModuleFileNameW(nullptr, exePath, MAX_PATH) == 0) return L".";
    std::wstring path(exePath);
    size_t pos = path.find_last_of(L"\\/");
    return (pos != std::wstring::npos) ? path.substr(0, pos) : L".";
}

// 默认 DLL 路径：<exeDir>\injected.dll
static std::wstring GetDefaultDllPath() {
    return GetExeDir() + L"\\injected.dll";
}

// char* -> wstring（用于 --dll / --pipe 参数）
static std::wstring ToWide(const char* s) {
    if (!s) return std::wstring();
    int len = MultiByteToWideChar(CP_UTF8, 0, s, -1, nullptr, 0);
    if (len <= 0) return std::wstring();
    std::wstring out(len - 1, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s, -1, out.data(), len);
    return out;
}

// 打印帮助
static void PrintHelp() {
    std::printf(
        "terminal-injector 0.1.0\n"
        "DLL 注入式终端劫持器，将已运行的控制台程序接管到 Windows Terminal\n\n"
        "用法:\n"
        "  terminal-injector.exe --inject <pid> [--dll <path>]\n"
        "      注入 injected.dll 到目标进程\n"
        "  terminal-injector.exe --mediator --target-pid <pid> [--pipe <name>] [--dll <path>]\n"
        "      中介模式（由 WT 启动），自动 fork 注入器并桥接 DLL 与 WT\n"
        "  terminal-injector.exe --unload-remote <pid> <dllBase>\n"
        "      远程卸载助手（Phase 11）：在目标进程创建远程线程调 FreeLibrary\n"
        "  terminal-injector.exe --list-targets [--json]\n"
        "      列出可注入的进程（权限 + x64 + 控制台程序），附原因标记\n"
        "  terminal-injector.exe --version\n"
        "  terminal-injector.exe --help\n\n"
        "参数:\n"
        "  --inject <pid>        注入模式，指定目标进程 PID\n"
        "  --mediator            中介模式\n"
        "  --target-pid <pid>    目标进程 PID（mediator 模式必需）\n"
        "  --dll <path>          injected.dll 路径，默认与 exe 同目录\n"
        "  --pipe <name>         命名管道名；缺省自动生成随机名（\\.\\.\\pipe\\terminjector_<pid>_<hex>）\n"
        "  --mediator-pid <pid>  管道服务端进程 PID（DLL 连接后校验身份，0=跳过）\n"
        "  --list-targets        列出可注入进程：PID + 进程名 + 状态\n"
        "  --json                与 --list-targets 搭配，输出 JSON 数组\n"
        "  --unload-remote <pid> <dllBase>\n"
        "                        远程卸载模式：远程 FreeLibrary(dllBase)\n"
        "  --version             显示版本号\n"
        "  --help, -h            显示此帮助\n");
}

// 解析命令行参数
static CliArgs ParseArgs(int argc, char* argv[]) {
    CliArgs args;
    args.dllPath = GetDefaultDllPath();

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--inject" && i + 1 < argc) {
            args.mode = CliArgs::Mode::Inject;
            try {
                args.targetPid = static_cast<uint32_t>(std::stoul(argv[++i]));
            } catch (...) {
                std::fprintf(stderr, "Invalid pid: %s\n", argv[i]);
                return args;
            }
        } else if (a == "--mediator") {
            args.mode = CliArgs::Mode::Mediator;
        } else if (a == "--list-targets") {
            args.mode = CliArgs::Mode::ListTargets;
        } else if (a == "--json") {
            args.json = true;
        } else if (a == "--unload-remote" && i + 2 < argc) {
            // --unload-remote <pid> <dllBase>
            // 由 injected.dll 的 Unloader 启动，独立于 WT 生命周期
            args.mode = CliArgs::Mode::UnloadRemote;
            try {
                args.targetPid = static_cast<uint32_t>(std::stoul(argv[++i]));
            } catch (...) {
                std::fprintf(stderr, "Invalid pid: %s\n", argv[i]);
                return args;
            }
            try {
                // dllBase 用 16 进制（0x...）或 10 进制均可
                args.dllBase = std::stoull(argv[++i], nullptr, 0);
            } catch (...) {
                std::fprintf(stderr, "Invalid dllBase: %s\n", argv[i]);
                return args;
            }
        } else if (a == "--target-pid" && i + 1 < argc) {
            try {
                args.targetPid = static_cast<uint32_t>(std::stoul(argv[++i]));
            } catch (...) {
                std::fprintf(stderr, "Invalid target-pid: %s\n", argv[i]);
                return args;
            }
        } else if (a == "--dll" && i + 1 < argc) {
            args.dllPath = ToWide(argv[++i]);
        } else if (a == "--pipe" && i + 1 < argc) {
            args.pipeName = ToWide(argv[++i]);
        } else if (a == "--mediator-pid" && i + 1 < argc) {
            try {
                args.mediatorPid = static_cast<uint32_t>(std::stoul(argv[++i]));
            } catch (...) {
                std::fprintf(stderr, "Invalid mediator-pid: %s\n", argv[i]);
                return args;
            }
        } else if (a == "--version") {
            args.mode = CliArgs::Mode::Version;
        } else if (a == "--help" || a == "-h") {
            args.mode = CliArgs::Mode::Help;
        } else {
            std::fprintf(stderr, "Unknown argument: %s (try --help)\n", argv[i]);
            return args;
        }
    }

    // mediator 模式校验：必须有 --target-pid
    if (args.mode == CliArgs::Mode::Mediator) {
        if (args.targetPid == 0) {
            std::fprintf(stderr, "Mediator mode requires --target-pid\n");
            return args;
        }
        // 管道名缺省生成随机后缀（安全加固：防同会话进程预创建抢占）
        // 运行链路不再使用 pid 约定名
        if (args.pipeName.empty()) {
            args.pipeName = MakeRandomPipeName(args.targetPid);
        }
    }

    // inject 模式校验：必须有 pid
    if (args.mode == CliArgs::Mode::Inject && args.targetPid == 0) {
        std::fprintf(stderr, "Inject mode requires <pid>\n");
        return args;
    }

    // unload-remote 模式校验：必须有 pid 和 dllBase
    if (args.mode == CliArgs::Mode::UnloadRemote) {
        if (args.targetPid == 0) {
            std::fprintf(stderr, "Unload-remote mode requires <pid>\n");
            return args;
        }
        if (args.dllBase == 0) {
            std::fprintf(stderr, "Unload-remote mode requires <dllBase>\n");
            return args;
        }
    }

    args.valid = true;
    return args;
}

// ============================================================
// Phase 11：远程卸载助手
// ============================================================
// 由 injected.dll 的 Unloader 在 DoUnload 末尾启动（独立进程），
// 在目标进程中创建远程线程调用 FreeLibrary(dllBase)。
//
// 为什么需要独立进程：
//   WT 关闭时 mediator 被连带终止，无法在 DLL DoUnload 后远程 FreeLibrary。
//   助手进程是 cmd 的子进程（DLL 在 cmd 中 CreateProcessW 启动它），
//   独立于 WT 生命周期，WT 关闭不影响它。
//
// 远程 FreeLibrary 能让 LoadCount 归 0 的原理：
//   调用线程是 CreateRemoteThread 新建的，从未进入 injected.dll 代码，
//   LDR 不为其持 ThreadBlob 引用。而 DLL 内部线程（含 DoUnload 线程）
//   曾在 injected.dll 中执行，LDR 保持 LoadCount=1 防止线程崩溃。
//   远程线程绕过此限制，FreeLibrary 后 LoadCount 真正归 0，触发 DETACH。
// ============================================================
// Phase 11：远程卸载助手辅助函数
// ============================================================

// 用 Toolhelp32 检查模块是否仍在目标进程中加载（按基址匹配）。
// 只读快照，不增加 LoadCount，比 PSAPI EnumProcessModulesEx 更轻量。
// 返回 true 表示模块仍在，false 表示已从模块列表消失。
static bool IsModuleLoaded(HANDLE hProc, uint64_t dllBase) {
    DWORD pid = GetProcessId(hProc);
    if (pid == 0) {
        // 无法获取 PID，保守返回 true（让调用方继续重试）
        return true;
    }
    HANDLE hSnap = CreateToolhelp32Snapshot(
        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid);
    if (hSnap == INVALID_HANDLE_VALUE) {
        // 快照失败，保守返回 true
        return true;
    }
    MODULEENTRY32W me{};
    me.dwSize = sizeof(me);
    bool found = false;
    if (Module32FirstW(hSnap, &me)) {
        do {
            if (reinterpret_cast<uint64_t>(me.modBaseAddr) == dllBase) {
                found = true;
                break;
            }
        } while (Module32NextW(hSnap, &me));
    }
    CloseHandle(hSnap);
    return found;
}

// 触发 LDR flush：远程调用 LoadLibraryW("kernel32.dll")。
// LdrLoadDll 内部调用 LdrpFlushUnloadCompleteProcessing 清理待卸载模块
// （State=9 LdrModulesReadyToUnload 的模块），让它们真正从 LdrLists 消失。
//
// 副作用：cmd 进程的 kernel32 LoadCount +1，但 kernel32 是系统核心 DLL
// 永不卸载，无实际影响。
static bool TriggerLdrFlush(HANDLE hProc) {
    HMODULE hKernel32 = GetModuleHandleW(L"kernel32.dll");
    if (!hKernel32) return false;
    auto pLoadLibraryW = reinterpret_cast<LPTHREAD_START_ROUTINE>(
        GetProcAddress(hKernel32, "LoadLibraryW"));
    if (!pLoadLibraryW) return false;

    // 在目标进程分配内存存放 "kernel32.dll" 宽字符串
    const wchar_t* dllName = L"kernel32.dll";
    SIZE_T pathLen = (wcslen(dllName) + 1) * sizeof(wchar_t);
    LPVOID remoteStr = VirtualAllocEx(hProc, nullptr, pathLen,
                                      MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
    if (!remoteStr) {
        LOG_ERROR("TriggerLdrFlush: VirtualAllocEx failed err=%lu", GetLastError());
        return false;
    }

    bool ok = false;
    SIZE_T written = 0;
    if (WriteProcessMemory(hProc, remoteStr, dllName, pathLen, &written)) {
        HANDLE hThread = CreateRemoteThread(hProc, nullptr, 0,
            pLoadLibraryW, remoteStr, 0, nullptr);
        if (hThread) {
            DWORD waitRet = WaitForSingleObject(hThread, 5000);
            DWORD exitCode = 0;
            GetExitCodeThread(hThread, &exitCode);
            LOG_INFO("TriggerLdrFlush: LoadLibraryW(kernel32) wait=%lu exitCode=0x%lx "
                     "(nonzero=success)",
                     waitRet, exitCode);
            CloseHandle(hThread);
            ok = (exitCode != 0);
        } else {
            LOG_ERROR("TriggerLdrFlush: CreateRemoteThread failed err=%lu",
                      GetLastError());
        }
    } else {
        LOG_ERROR("TriggerLdrFlush: WriteProcessMemory failed err=%lu",
                  GetLastError());
    }
    VirtualFreeEx(hProc, remoteStr, 0, MEM_RELEASE);
    return ok;
}

static int RunUnloadRemote(uint32_t targetPid, uint64_t dllBase) {
    LOG_INFO("UnloadRemote: targetPid=%u dllBase=0x%llx",
             targetPid, static_cast<unsigned long long>(dllBase));

    // 1. 打开目标进程（需 CREATE_THREAD/VM_OPERATION 等权限）
    DWORD access = PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
                   PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ;
    HANDLE hProc = OpenProcess(access, FALSE, targetPid);
    if (!hProc) {
        LOG_ERROR("UnloadRemote: OpenProcess failed pid=%u err=%lu",
                  targetPid, GetLastError());
        return 1;
    }

    // 2. 取 kernel32!FreeLibrary 地址
    //    系统共享基址保证此地址在目标进程中也是 FreeLibrary
    HMODULE hKernel32 = GetModuleHandleW(L"kernel32.dll");
    if (!hKernel32) {
        LOG_ERROR("UnloadRemote: GetModuleHandleW(kernel32) failed err=%lu",
                  GetLastError());
        CloseHandle(hProc);
        return 1;
    }
    auto pFreeLibrary = reinterpret_cast<LPTHREAD_START_ROUTINE>(
        GetProcAddress(hKernel32, "FreeLibrary"));
    if (!pFreeLibrary) {
        LOG_ERROR("UnloadRemote: GetProcAddress(FreeLibrary) failed err=%lu",
                  GetLastError());
        CloseHandle(hProc);
        return 1;
    }

    // 3. 等 DoUnload 线程 + Logger worker 线程退出
    //    助手进程在 Unloader 步骤 9 启动，此时：
    //      - DoUnload 线程仍在步骤 9 中执行（还没到步骤 11 ExitThread）
    //      - Logger worker 线程在运行（步骤 10 Logger::Shutdown 还没执行）
    //    LoadCount 此时 >= 3（DoUnload 线程 + Logger 线程 + cmd 主线程 ThreadBlob）
    //    若此时远程 FreeLibrary 只减 1，LoadCount 未归零，DETACH 不触发。
    //
    //    需等它们退出后，LoadCount 才降到 1（只剩 cmd 主线程 LdrpThreadBlob），
    //    远程 FreeLibrary 才能把 LoadCount 减到 0，触发 DLL_PROCESS_DETACH。
    //
    //    等待时间估算：
    //      - 步骤 10 Logger::Shutdown 同步等 Logger worker 退出（RingBufferLogger
    //        m_shutdown=true + WaitForSingleObject，~100-200ms）
    //      - 步骤 11 ExitThread 立即返回
    //      - LDR 释放 DoUnload 线程的 ThreadBlob（~100ms）
    //    共 ~500ms，保守取 2000ms 留足余量
    LOG_INFO("UnloadRemote: waiting 2000ms for DoUnload + Logger threads to exit "
             "and LDR to release ThreadBlobs");
    Sleep(2000);

    // 4. 重试循环：远程 FreeLibrary + LDR flush
    //    首次 FreeLibrary 应让 LoadCount 归 0 触发 DETACH，但 LDR 可能延迟卸载
    //    （State=9 LdrModulesReadyToUnload），模块仍在 LdrLists。
    //    需触发 LDR flush（远程 LoadLibraryW）才会真正从模块列表消失。
    //    重试 3 次应对时序不确定（ThreadBlob 释放延迟等）。
    bool unloaded = false;
    for (int attempt = 1; attempt <= 3; ++attempt) {
        // 远程 FreeLibrary(dllBase)
        HANDLE hThread = CreateRemoteThread(
            hProc, nullptr, 0,
            pFreeLibrary,
            reinterpret_cast<LPVOID>(static_cast<uintptr_t>(dllBase)),
            0, nullptr);
        if (!hThread) {
            LOG_ERROR("UnloadRemote: attempt %d CreateRemoteThread failed err=%lu",
                      attempt, GetLastError());
            // 等待后重试（可能是临时资源问题）
            Sleep(500);
            continue;
        }
        DWORD waitRet = WaitForSingleObject(hThread, 5000);
        DWORD exitCode = 0;
        GetExitCodeThread(hThread, &exitCode);
        LOG_INFO("UnloadRemote: attempt %d FreeLibrary wait=%lu exitCode=%lu "
                 "(nonzero=success) dllBase=0x%llx",
                 attempt, waitRet, exitCode,
                 static_cast<unsigned long long>(dllBase));
        CloseHandle(hThread);

        // 等 LDR 处理 DETACH（DllMain MH_Uninitialize 等）
        Sleep(300);

        // 检查模块是否还在
        if (!IsModuleLoaded(hProc, dllBase)) {
            LOG_INFO("UnloadRemote: DLL unloaded after FreeLibrary (attempt %d)",
                     attempt);
            unloaded = true;
            break;
        }

        // 模块仍在：可能是 LoadCount 未归零 或 LDR 延迟卸载（State=9）
        // 触发 LDR flush 让 LdrpFlushUnloadCompleteProcessing 清理待卸载模块
        LOG_INFO("UnloadRemote: DLL still loaded after attempt %d, triggering LDR flush",
                 attempt);
        TriggerLdrFlush(hProc);
        Sleep(300);

        if (!IsModuleLoaded(hProc, dllBase)) {
            LOG_INFO("UnloadRemote: DLL unloaded after LDR flush (attempt %d)",
                     attempt);
            unloaded = true;
            break;
        }
    }

    CloseHandle(hProc);
    LOG_INFO("UnloadRemote: final result unloaded=%d dllBase=0x%llx",
             unloaded, static_cast<unsigned long long>(dllBase));
    return unloaded ? 0 : 1;
}

// ============================================================
// --list-targets：列出可注入进程
// ============================================================
// 判定由 ProcessHelper::EnumerateInjectTargets 完成：
//   排除自身/系统进程 → OpenProcess（注入权限）→ x64 → PE CUI（控制台程序）
// 文本输出 Tab 分隔（PID / 进程名 / 状态），JSON 输出供脚本化使用。
namespace {

// JSON 字符串转义（进程名可能含引号/反斜杠/控制字符）
std::string EscapeJson(const std::wstring& s) {
    std::string out;
    out.reserve(s.size() * 2 + 2);
    for (wchar_t c : s) {
        switch (c) {
            case L'"':  out += "\\\""; break;
            case L'\\': out += "\\\\"; break;
            case L'\n': out += "\\n"; break;
            case L'\r': out += "\\r"; break;
            case L'\t': out += "\\t"; break;
            default:
                // 非 ASCII 字符按 UTF-8 逐字节输出（保持字节序小端）
                if (c < 0x20) {
                    char buf[8];
                    std::snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else {
                    char mb[4] = {0};
                    // 用 WideCharToMultiByte 转为 UTF-8，避免直接截断 wchar
                    int n = WideCharToMultiByte(CP_UTF8, 0, &c, 1, mb, 4,
                                                nullptr, nullptr);
                    out.append(mb, n > 0 ? n : 1);
                }
        }
    }
    return out;
}

// 生成单条状态文本：可注入 -> injectable；否则为原因标记
const wchar_t* StatusText(const ProcessHelper::InjectTargetInfo& info) {
    if (info.injectable) return L"injectable";
    return info.reason.c_str();
}

int RunListTargets(bool json) {
    // 提升 SeDebugPrivilege：管理员运行时能判定更多进程（失败不阻塞，
    // 同权限进程不受影响；权限不足的系统进程显示 access_denied）
    ProcessHelper::EnableDebugPrivilege();

    auto targets = ProcessHelper::EnumerateInjectTargets();

    if (json) {
        std::string out = "[";
        bool first = true;
        for (const auto& t : targets) {
            if (!first) out += ",";
            first = false;
            out += "{\"pid\":" + std::to_string(t.pid) +
                   ",\"name\":\"" + EscapeJson(t.name) + "\"" +
                   ",\"injectable\":" + (t.injectable ? "true" : "false") +
                   ",\"x64\":" + (t.x64 ? "true" : "false") +
                   ",\"console\":" + (t.cui ? "true" : "false") +
                   ",\"already_injected\":" +
                   (t.alreadyInjected ? "true" : "false") +
                   ",\"reason\":\"" +
                   (t.reason.empty() ? std::string("") : EscapeJson(t.reason)) +
                   "\"" +
                   ",\"start_time\":\"" + EscapeJson(t.startTime) + "\"" +
                   ",\"cmd_line\":\"" + EscapeJson(t.cmdLine) + "\"" +
                   "}";
        }
        out += "]";
        std::printf("%s\n", out.c_str());
        return 0;
    }

    // 文本输出：可注入在前，其后按 PID 升序
    std::vector<ProcessHelper::InjectTargetInfo> injectable;
    std::vector<ProcessHelper::InjectTargetInfo> others;
    for (const auto& t : targets) {
        if (t.injectable) injectable.push_back(t);
        else others.push_back(t);
    }
    auto byPid = [](const ProcessHelper::InjectTargetInfo& a,
                    const ProcessHelper::InjectTargetInfo& b) {
        return a.pid < b.pid;
    };
    std::sort(injectable.begin(), injectable.end(), byPid);
    std::sort(others.begin(), others.end(), byPid);

    std::printf("PID\tNAME\tSTATUS\n");
    for (const auto& t : injectable) {
        if (t.alreadyInjected) {
            std::printf("%u\t%ls\tinjectable (already injected)\n",
                        t.pid, t.name.c_str());
        } else {
            std::printf("%u\t%ls\tinjectable\n", t.pid, t.name.c_str());
        }
    }
    for (const auto& t : others) {
        std::printf("%u\t%ls\t%ls\n", t.pid, t.name.c_str(), StatusText(t));
    }
    return 0;
}

} // namespace

// 主分发逻辑
static int Run(int argc, char* argv[]) {
    // 先解析参数，再根据 mode 决定日志路径（mediator/inject 按目标 pid 分文件）。
    // 原因：--unload-remote 助手进程由 DLL 在 DoUnload 末尾启动，
    // 与下一次循环的 mediator 并发。若共用同一日志文件，会因
    // FILE_SHARE_READ 不允许 share write 而冲突（CreateFileW 失败，
    // 日志写不进，wait_for_handshake 超时）。
    // --unload-remote 写独立日志文件（terminal-injector-unload.log），
    // mediator 按 pid 分文件（terminal-injector-<pid>.log），互不抢占。
    auto args = ParseArgs(argc, argv);

    // 用 exe 同目录的绝对路径写日志，避免 WT 启动时工作目录不确定
    // 否则相对路径 "terminal-injector.log" 可能写到不可预测的位置
    std::wstring exeDir = GetExeDir();
    std::wstring logPath;
    if (args.mode == CliArgs::Mode::UnloadRemote) {
        // 助手进程独立日志：terminal-injector-unload.log
        // （--unload-remote 由 DLL 在 DoUnload 末尾启动，与后续 mediator 并发）
        logPath = exeDir + L"\\terminal-injector-unload.log";
    } else if (args.mode == CliArgs::Mode::Mediator) {
        // mediator 按目标 pid 分文件：terminal-injector-<pid>.log
        // - 与 DLL 侧 injected_<pid>.log 约定对齐，按会话归档定位
        // - 并发 mediator（上次循环残留 + 本次）各写独立文件，不再互抢句柄
        logPath = exeDir + L"\\terminal-injector-" + std::to_wstring(args.targetPid) + L".log";
    } else if (args.mode == CliArgs::Mode::Inject) {
        // 注入器进程与 mediator 并发运行且 targetPid 相同，若共用
        // terminal-injector-<pid>.log（Logger 不 share write）会互斥失败、
        // 注入器日志全部丢失 → 注入器独立日志文件
        logPath = exeDir + L"\\terminal-injector-inject-" +
                  std::to_wstring(args.targetPid) + L".log";
    } else {
        // Help/Version 等模式：terminal-injector.log
        logPath = exeDir + L"\\terminal-injector.log";
    }
    Logger::Initialize(logPath.c_str(), LogLevel::Debug);
    LOG_INFO("=== terminal-injector starting, argc=%d ===", argc);
    for (int i = 0; i < argc; ++i) {
        LOG_INFO("argv[%d] = %s", i, argv[i]);
    }

    if (!args.valid) {
        Logger::Shutdown();
        return 1;
    }

    int ret = 0;
    switch (args.mode) {
        case CliArgs::Mode::Inject: {
            LOG_INFO("Mode=Inject, pid=%u dll=%ls",
                     args.targetPid, args.dllPath.c_str());
            Injector injector;
            // 管道名缺省生成随机名（由 mediator fork 时经 --pipe 传入；
            // 手动 --inject 无 --pipe 时生成随机名，DLL 收到后无对应服务端，
            // 连接失败属预期——手动模式本来就没有 mediator）
            std::wstring pipeName = args.pipeName.empty()
                                        ? MakeRandomPipeName(args.targetPid)
                                        : args.pipeName;
            if (!injector.Inject(args.targetPid, args.dllPath,
                                 pipeName, args.mediatorPid)) {
                LOG_ERROR("Inject failed");
                std::fprintf(stderr, "Inject failed, see terminal-injector.log\n");
                ret = 1;
            } else {
                std::printf("Inject succeeded, pid=%u\n", args.targetPid);
            }
            break;
        }
        case CliArgs::Mode::Mediator: {
            LOG_INFO("Mode=Mediator, targetPid=%u pipe=%ls dll=%ls mediatorPid=%u",
                     args.targetPid, args.pipeName.c_str(), args.dllPath.c_str(),
                     GetCurrentProcessId());
            Mediator mediator;
            // mediatorPid 固定为自身 pid：DLL 连接后校验服务端进程身份
            ret = mediator.Run(args.targetPid, args.pipeName, args.dllPath,
                               GetCurrentProcessId());
            break;
        }
        case CliArgs::Mode::UnloadRemote: {
            LOG_INFO("Mode=UnloadRemote, targetPid=%u dllBase=0x%llx",
                     args.targetPid,
                     static_cast<unsigned long long>(args.dllBase));
            ret = RunUnloadRemote(args.targetPid, args.dllBase);
            break;
        }
        case CliArgs::Mode::ListTargets: {
            LOG_INFO("Mode=ListTargets json=%d", args.json ? 1 : 0);
            ret = RunListTargets(args.json);
            break;
        }
        case CliArgs::Mode::Help:
            PrintHelp();
            break;
        case CliArgs::Mode::Version:
            std::printf("terminal-injector 0.1.0\n");
            break;
        default:
            std::fprintf(stderr, "No mode specified. Use --help.\n");
            ret = 1;
            break;
    }

    LOG_INFO("=== terminal-injector exit, code=%d ===", ret);
    Logger::Shutdown();
    return ret;
}

} // namespace terminjector

int main(int argc, char* argv[]) {
    return terminjector::Run(argc, argv);
}
