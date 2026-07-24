// injected.dll 入口
// 详见 docs/phases/03-dll-framework.md 4.1
//
// Phase 3 改造：DllMain 极简，重活交给懒加载
//   - DLL_PROCESS_ATTACH: DisableThreadLibraryCalls + MH_Initialize
//                         + RegisterOutputHooks + InstallAll（启用 Hook 提供触发器）
//   - 首个 Hook Detour 触发 EnsureLazyInitialized（Logger/Capture/Connect/State）
//   - DLL_PROCESS_DETACH: UninstallAll + MH_Uninitialize + 释放通道 + Logger::Shutdown
//
// Loader Lock 注意事项：
//   - DllMain 内禁止 GetConsoleScreenBufferInfo 等 Console API（留给懒加载）
//   - 禁止 CreateThread（用懒加载避免）
//   - MH_Initialize / Register / InstallAll 仅调 MinHook 与 GetProcAddress，
//     不触发 Loader Lock
//   - Logger 未初始化前用 OutputDebugStringW 兜底
#include <windows.h>
#include <MinHook.h>

#include "logging/Logger.h"
#include "HookManager.h"
#include "LazyInit.h"
#include "DllRecvLoop.h"
#include "hooks/OutputHooks.h"
#include "hooks/CursorHooks.h"
#include "hooks/BufferHooks.h"
#include "hooks/ProcessHooks.h"  // Phase 12：子进程注入
#include "hooks/InputHooks.h"    // Phase 6：输入注入
#include "hooks/ModeHooks.h"     // Phase 7：模式欺骗
#include "hooks/FontHooks.h"     // Phase 8：字体缓存
#include "hooks/WaitHooks.h"     // Phase 8：Wait 句柄假映射
#include "hooks/ProtectionHooks.h"  // Phase 9：防越狱 + CloseHandle 静默
#include "state/StatePoller.h"     // Phase 10：后台状态轮询
#include "exports.h"

namespace {
HMODULE g_hModule = nullptr;
}

// === 调试探针（Phase 3 端到端验证用）===
// extern "C" 保证符号名不修饰，cdb 可直接 dd injected!g_probe_dllmain 读取
extern "C" volatile LONG g_probe_dllmain = 0;

// 导出函数：查询 DLL 版本
// 注入器/mediator 可远程调用此函数验证 DLL 是我们的 injected.dll
// 返回静态字符串指针，无需释放
extern "C" __declspec(dllexport) const char* Inject_QueryVersion(void) {
    return "0.1.0";
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID /*reserved*/) {
    switch (reason) {
        case DLL_PROCESS_ATTACH: {
            g_hModule = hModule;
            // 禁用线程 attach/detach 通知，减少 DllMain 调用次数
            DisableThreadLibraryCalls(hModule);

            // 1. 初始化 MinHook（失败则拒绝加载）
            if (MH_Initialize() != MH_OK) {
                OutputDebugStringW(L"[terminjector] DllMain: MH_Initialize failed");
                return FALSE;
            }
            OutputDebugStringW(L"[terminjector] DllMain: MH initialized");

            // 2. 注册输出类 Hook（WriteConsoleW/A）
            //    注意：此时 Logger 未初始化，RegisterOutputHooks 内的日志不落盘
            terminjector::hooks::RegisterOutputHooks();
            // Phase 5：注册光标/缓冲区类 Hook
            terminjector::hooks::RegisterCursorHooks();
            terminjector::hooks::RegisterBufferHooks();
            // Phase 12：注册进程创建类 Hook（CreateProcessW/A）
            //           拦截子进程创建并自动注入 DLL
            terminjector::hooks::RegisterProcessHooks();
            // Phase 6：注册输入类 Hook（ReadConsoleInput/Peek/WriteConsoleInput/
            //          GetNumberOfConsoleInputEvents/FlushConsoleInputBuffer/ReadFile）
            //          拦截目标程序输入读取，从 InputQueue 取数据返回
            terminjector::hooks::RegisterInputHooks();
            // Phase 7：注册模式类 Hook
            //          GetConsoleMode（欺骗 VT 已开启）+ SetConsoleMode（状态机）
            //          Ctrl+C 信号由 DllRecvLoop → TriggerCtrlC → GenerateConsoleCtrlEvent 处理
            terminjector::hooks::RegisterModeHooks();
            // Phase 8：注册字体类 Hook（GetCurrentConsoleFontEx 等，返回缓存）
            terminjector::hooks::RegisterFontHooks();
            // Phase 8：注册 Wait 句柄类 Hook（GetConsoleInputWaitHandle → InputQueue 事件）
            terminjector::hooks::RegisterWaitHooks();
            // Phase 9：注册自保护类 Hook（Alloc/Attach/Free/CloseHandle）
            //          阻止目标程序脱离中介管道，假句柄静默返回
            terminjector::hooks::RegisterProtectionHooks();

            // 3. 安装并启用全部 Hook（提供懒加载触发器）
            //    首个被拦截的 Console API 调用会触发 EnsureLazyInitialized
            if (!terminjector::HookManager::InstallAll()) {
                OutputDebugStringW(L"[terminjector] DllMain: InstallAll failed");
                // 不拒绝加载：Hook 失败时目标程序仍可正常运行（无劫持）
            }
            // === 调试探针：确认 DllMain ATTACH 执行完毕 ===
            InterlockedIncrement(&g_probe_dllmain);
            OutputDebugStringW(L"[terminjector-probe] DllMain ATTACH done, hooks installed");

            // 4. 主动触发懒加载的工作线程
            //    原因：若目标进程注入后不主动调用输出类 API（如 cmd.exe 注入前
            //    已输出提示符，注入后阻塞在 ReadConsole 等输入），LazyInit 永远
            //    不会触发，导致 mediator 卡在 WaitClient，WT 输入传不进来，死锁。
            //    方案：DllMain 里 CreateThread（不触发 Loader Lock，新线程在
            //    DllMain 返回后才开始执行），延迟 100ms 调 EnsureLazyInitialized
            //    主动建连，打破"等 cmd 输出才触发"的依赖。
            HANDLE hWorker = CreateThread(
                nullptr, 0,
                [](LPVOID) -> DWORD {
                    Sleep(100);  // 等 DllMain 返回，避免 Loader Lock
                    terminjector::EnsureLazyInitialized();
                    // Phase 6：唤醒阻塞在原 ReadConsoleW 的目标线程
                    // cmd 在 Hook 安装前已进入 ReadConsoleW 阻塞，
                    // 向 ConHost 写回车键让它返回，下次调用走 Detour
                    //
                    // 仅对注入目标进程执行：子进程是注入后由父进程 CreateProcess
                    // 创建的，Hook 已就位，不存在旧 ReadConsoleW 阻塞；若对子进程
                    // KickStart，ENTER 会残留 ConHost 队列，被子进程后续 ReadConsoleW
                    // 误读（如 Python REPL 触发空行 → 第二个 >>>）
                    if (terminjector::IsLazyInitialized() &&
                        terminjector::IsTargetProcess()) {
                        terminjector::hooks::KickStartBlockedReaders();
                    }
                    return 0;
                },
                nullptr, 0, nullptr);
            if (hWorker) {
                CloseHandle(hWorker);
            }
            break;
        }
        case DLL_PROCESS_DETACH: {
            // Phase 10：先停止 StatePoller 轮询线程，避免线程在 transport/Hook
            // 释放后仍调 SendToMediator / CallRealGetConsoleScreenBufferInfo
            terminjector::StatePoller::Instance().Stop();

            // Phase 5：先停止接收线程，再卸载 Hook 与 transport
            terminjector::StopDllRecvLoop();

            // 卸载 Hook（恢复原 API）
            if (terminjector::HookManager::IsInstalled()) {
                terminjector::HookManager::UninstallAll();
            }
            MH_Uninitialize();

            // 释放 mediator 传输通道
            terminjector::ReleaseMediatorTransport();

            // 若 Logger 已初始化（懒加载执行过），关闭日志
            if (terminjector::Logger::IsInitialized()) {
                LOG_INFO("=== injected.dll unloaded, pid=%lu ===",
                         GetCurrentProcessId());
                terminjector::Logger::Shutdown();
            }
            break;
        }
        default:
            break;
    }
    return TRUE;
}
