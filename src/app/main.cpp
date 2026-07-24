// terminal-injector 双模式入口
// 详见 docs/phases/02-injector-modes.md 4.2 / 4.3
//
// 用法：
//   terminal-injector.exe --inject <pid> [--dll <path>]
//       注入模式：将 injected.dll 注入到 <pid> 进程
//   terminal-injector.exe --mediator --target-pid <pid> [--pipe <name>] [--dll <path>]
//       中介模式：作为 WT 子进程，建立管道等待 DLL 连接
//   terminal-injector.exe --version
//   terminal-injector.exe --help
//
// 参数解析手写（不引入第三方 CLI 库），保持依赖最小
#include "logging/Logger.h"
#include "transport/ITransport.h"      // MakePipeName
#include "injector/Injector.h"
#include "mediator/Mediator.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>
#include <windows.h>

namespace terminjector {

// 命令行参数
struct CliArgs {
    enum class Mode { None, Inject, Mediator, Help, Version };
    Mode         mode = Mode::None;
    uint32_t     targetPid = 0;
    std::wstring dllPath;    // 默认 exe 同目录的 injected.dll
    std::wstring pipeName;   // 默认根据 targetPid 构造

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
        "  terminal-injector.exe --version\n"
        "  terminal-injector.exe --help\n\n"
        "参数:\n"
        "  --inject <pid>        注入模式，指定目标进程 PID\n"
        "  --mediator            中介模式\n"
        "  --target-pid <pid>    目标进程 PID（mediator 模式必需）\n"
        "  --dll <path>          injected.dll 路径，默认与 exe 同目录\n"
        "  --pipe <name>         命名管道名，默认 \\\\.\\pipe\\terminjector_<pid>\n"
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
        // 默认管道名
        if (args.pipeName.empty()) {
            args.pipeName = MakePipeName(args.targetPid);
        }
    }

    // inject 模式校验：必须有 pid
    if (args.mode == CliArgs::Mode::Inject && args.targetPid == 0) {
        std::fprintf(stderr, "Inject mode requires <pid>\n");
        return args;
    }

    args.valid = true;
    return args;
}

// 主分发逻辑
static int Run(int argc, char* argv[]) {
    // 用 exe 同目录的绝对路径写日志，避免 WT 启动时工作目录不确定
    // 否则相对路径 "terminal-injector.log" 可能写到不可预测的位置
    std::wstring exeDir = GetExeDir();
    std::wstring logPath = exeDir + L"\\terminal-injector.log";
    Logger::Initialize(logPath.c_str(), LogLevel::Debug);
    LOG_INFO("=== terminal-injector starting, argc=%d ===", argc);
    for (int i = 0; i < argc; ++i) {
        LOG_INFO("argv[%d] = %s", i, argv[i]);
    }

    auto args = ParseArgs(argc, argv);
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
            // 注入模式：管道名仅用于日志（DLL 自发现约定）
            std::wstring pipeName = MakePipeName(args.targetPid);
            if (!injector.Inject(args.targetPid, args.dllPath, pipeName)) {
                LOG_ERROR("Inject failed");
                std::fprintf(stderr, "Inject failed, see terminal-injector.log\n");
                ret = 1;
            } else {
                std::printf("Inject succeeded, pid=%u\n", args.targetPid);
            }
            break;
        }
        case CliArgs::Mode::Mediator: {
            LOG_INFO("Mode=Mediator, targetPid=%u pipe=%ls dll=%ls",
                     args.targetPid, args.pipeName.c_str(), args.dllPath.c_str());
            Mediator mediator;
            ret = mediator.Run(args.targetPid, args.pipeName, args.dllPath);
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
