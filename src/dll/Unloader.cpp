// DLL 主动卸载器实现（Phase 11）
// 详见 docs/phases/11-unload-testing.md 4.2
//
// DoUnload 流程（顺序敏感，避免线程竞态）：
//   1. StatePoller.Stop       停后台轮询，避免轮询线程在 Hook 卸载后仍调 SendToMediator
//   2. BatchSender.Shutdown   flush 最后一批 VtOutput（需 transport 仍可用）
//   3. InputQueue.SignalDataReady  唤醒阻塞在 ReadConsoleInput 上的线程
//      （Hook 即将卸载，唤醒后下次调用走原 API，不再依赖 InputQueue）
//  3.5 等待 s_active_read_detours 归零（2s 超时强制继续）
//      关键：Read 类 Detour 内 pass-through 调 *_orig（trampoline）前必须先 LeaveReadDetour，
//      否则 UninstallAll 释放 trampoline 时线程还在调用 → AV 0xC0000005
//   4. HookManager.UninstallAll   卸载所有 Hook（之后 Console API 走原 API）
//   5. 恢复 ConsoleState          用真实 API 读 ConHost 状态写回缓存
//      （目标程序下次 GetConsoleScreenBufferInfo 拿到 ConHost 真值而非过期缓存）
//   6. 显示原 Console 窗口        Phase 9 隐藏过，恢复可见
//   7. 发送 UnloadComplete 给 mediator  辅助路径（transport 仍可用时）
//      mediator 收到后远程 FreeLibrary（仅在 mediator 存活时有效）
//   8. ReleaseMediatorTransport   断开命名管道（发完 UnloadComplete 才断）
//   9. 启动助手进程 terminal_injector.exe --unload-remote
//      主路径：助手进程独立于 WT 生命周期，远程 FreeLibrary(dllBase)
//      WT 关闭时 mediator 被连带杀死，UnloadComplete 发不出去（pipe 已断），
//      必须用独立进程触发远程 FreeLibrary
//  10. Logger::Shutdown           停止 RingBufferLogger worker 线程（必须在退出线程前）
//      原因：worker 线程在 injected.dll 代码中执行，若不停止，LoadCount 无法归零，
//      远程 FreeLibrary 后 DETACH 不触发，形成循环依赖（详见步骤 10 注释）
//  11. ExitThread(0)              退出卸载线程（不调 FreeLibraryAndExitThread）
//      卸载线程退出后 LDR 释放其 ThreadBlob，远程 FreeLibrary 才能把 LoadCount 减到 0
//
// 为什么需要助手进程（2026-07-25 诊断）：
//   WT 关闭时 mediator 被连带终止，DLL 检测 pipe 断开触发 DoUnload，
//   但此时 pipe 已断，UnloadComplete 发不出去（err=232 ERROR_NO_DATA）。
//   助手进程是 cmd 的子进程（DLL 在 cmd 中 CreateProcessW 启动它），
//   独立于 WT 生命周期，能在 DoUnload 完成后远程 FreeLibrary。
//
// 为什么 DLL 内部不能自己 FreeLibrary（2026-07-25 诊断）：
//   实测 FreeLibraryAndExitThread 把 LoadCount 从 5 减到 1，剩余 1 个引用来自
//   cmd 主线程的 LdrpThreadBlob（cmd 调过被 Hook 的 API，LDR 跟踪"该线程曾进入
//   injected.dll"，Hook 卸载后该跟踪记录未释放）。cdb 确认无线程在 injected.dll
//   代码中执行，State=9 (LdrModulesReadyToUnload)，但 DETACH 未触发。
//   CreateThread+FreeLibrary 也失败：新线程入口虽是 kernel32!FreeLibrary，
//   但 LDR 仍因 cmd 主线程 ThreadBlob 持引用。
//   根因：要让 LoadCount 归 0 触发 DETACH，必须由一个"从未进入过 injected.dll"
//   的线程调用 FreeLibrary——助手进程通过 CreateRemoteThread 创建的远程线程满足此条件。
//
// 与 DllMain DETACH 的协作：
//   - s_unloading=true 让 DETACH 跳过大部分清理（已在 DoUnload 完成）
//   - DETACH 仍做 MH_Uninitialize（MinHook 要求在 DETACH 中调用）
//   - 幂等保护：所有 Stop/Shutdown/Uninstall 方法重复调用安全
#include "Unloader.h"
#include "LazyInit.h"
#include "HookManager.h"
#include "DllRecvLoop.h"
#include "state/StatePoller.h"
#include "state/ConsoleState.h"
#include "state/InputQueue.h"
#include "BatchSender.h"
#include "logging/Logger.h"
#include "protocol/Message.h"
#include "protocol/MessageSerializer.h"
#include "transport/ITransport.h"

#include <windows.h>
#include <thread>
#include <cstdint>
#include <string>
#include <vector>

namespace terminjector {

std::atomic<bool> Unloader::s_unloading{false};
std::atomic<int>  Unloader::s_active_read_detours{0};

void Unloader::RequestUnload() {
    if (s_unloading.exchange(true)) {
        // 已有卸载在进行，直接返回
        return;
    }
    // 在独立线程执行卸载，避免在 recv 线程或 Hook 线程中死锁
    // （DoUnload 会 UninstallAll，若在 Hook 线程中执行会卸载自己正在执行的代码）
    std::thread([] {
        DoUnload();
    }).detach();
}

void Unloader::DoUnload() {
    LOG_INFO("Unload starting, pid=%lu", GetCurrentProcessId());

    // 1. 停止后台状态轮询（避免轮询线程在 Hook 卸载后仍调 CallReal* / SendToMediator）
    StatePoller::Instance().Stop();

    // 2. 停止 BatchSender 并 flush 最后一批 VtOutput
    //    必须在 ReleaseMediatorTransport 之前：Shutdown 内部会做最终 flush，flush 需要 transport
    BatchSender::Instance().Shutdown();

    // 3. 唤醒阻塞在 InputQueue 上的读取线程
    //    Hook 即将卸载，唤醒后 ReadConsoleInput 会返回（无数据），下次调用走原 API
    InputQueue::Instance().SignalDataReady();

    // 3.5 等待所有 Read 类 Detour 线程离开 DLL 代码（2026-07-25 修复 cmd AV 崩溃）
    //     根因：SignalDataReady 唤醒主线程后立即 UninstallAll 会释放 trampoline，
    //           主线程被调度回来调用 ReadConsoleW_orig（trampoline）→ AV 0xC0000005
    //     修复：Detour 入口 EnterReadDetour（计数++），
    //           pass-through 调 orig 前 LeaveReadDetour（计数--，让 Unloader 可继续）
    //     超时 2s 兜底防死锁（极端情况下 Detour 卡住，仍要继续卸载）
    {
        constexpr int kWaitTotalMs = 2000;
        constexpr int kSleepStepMs = 10;
        int waited = 0;
        int lastCount = -1;
        while (waited < kWaitTotalMs) {
            int n = ActiveReadDetours();
            if (n == 0) {
                LOG_INFO("Unload: all read detours exited (waited %dms)", waited);
                break;
            }
            if (n != lastCount) {
                LOG_INFO("Unload: waiting %d read detour(s) to exit (waited %dms)", n, waited);
                lastCount = n;
            }
            Sleep(kSleepStepMs);
            waited += kSleepStepMs;
            // 持续唤醒：避免有线程错过第一次 SetEvent 信号
            InputQueue::Instance().SignalDataReady();
        }
        if (ActiveReadDetours() != 0) {
            LOG_WARN("Unload: %d read detour(s) still active after %dms, force uninstall (may crash)",
                     ActiveReadDetours(), kWaitTotalMs);
        }
    }

    // 4. 卸载所有 Hook（恢复原 API 字节）
    //    之后目标程序调 Console API 直接走系统 API，不再进 Detour
    HookManager::UninstallAll();

    // 5. 恢复 ConsoleState 到真实 ConHost 状态
    //    Hook 已卸载，GetConsoleScreenBufferInfo 直接读 ConHost 真值
    //    写回 ConsoleState 缓存，让目标程序下次查询拿到 ConHost 真值
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO info{};
    if (GetConsoleScreenBufferInfo(hOut, &info)) {
        ConsoleState::Instance().SetBufferSize(info.dwSize);
        ConsoleState::Instance().SetCursorPosition(info.dwCursorPosition);
        ConsoleState::Instance().SetWindow(info.srWindow);
        LOG_INFO("Unload: ConsoleState restored buf=(%d,%d) cursor=(%d,%d) win=(%d,%d,%d,%d)",
                 info.dwSize.X, info.dwSize.Y,
                 info.dwCursorPosition.X, info.dwCursorPosition.Y,
                 info.srWindow.Left, info.srWindow.Top,
                 info.srWindow.Right, info.srWindow.Bottom);
    } else {
        LOG_WARN("Unload: GetConsoleScreenBufferInfo failed err=%lu", GetLastError());
    }

    // 6. 显示原 Console 窗口（Phase 9 隐藏过，恢复可见让用户能继续操作）
    HWND hCon = GetConsoleWindow();
    if (hCon) {
        ShowWindow(hCon, SW_SHOW);
        LOG_INFO("Unload: console window shown");
    }

    // 7. 发送 UnloadComplete 给 mediator（辅助路径）
    //    必须在 ReleaseMediatorTransport 之前：发送需要 transport 仍可用
    //    仅在 mediator 仍存活时有效（如 mediator 主动发 Shutdown 触发卸载）
    //    WT 关闭场景下 mediator 已被杀死，pipe 断开，此处发送会失败（err=232）
    //    失败不影响主路径（步骤 9 启动助手进程）
    {
        ITransport* transport = GetMediatorTransport();
        if (transport && transport->IsConnected()) {
            auto pkt = protocol::Serialize(
                protocol::MessageType::UnloadComplete, nullptr, 0);
            const int sent = transport->Send(pkt.data(), pkt.size());
            LOG_INFO("Unload: sent UnloadComplete to mediator, sent=%d/%zu",
                     sent, pkt.size());
        } else {
            LOG_INFO("Unload: transport unavailable, skip UnloadComplete "
                     "(helper process will handle remote FreeLibrary)");
        }
    }

    // 8. 释放 mediator 传输通道（断开命名管道）
    //    之后 SendToMediator 调用会因 transport=nullptr 直接返回 false
    ReleaseMediatorTransport();

    // 9. 启动助手进程远程 FreeLibrary（主路径）
    //    助手进程独立于 WT 生命周期，能在 WT 关闭后存活
    //    命令行：terminal_injector.exe --unload-remote <pid> <dllBase>
    //    助手进程会 OpenProcess + CreateRemoteThread(FreeLibrary, dllBase)
    //    必须在 Logger::Shutdown 之前启动：启动失败需记录日志
    {
        HMODULE hSelf = GetModuleHandleW(L"injected.dll");
        if (hSelf) {
            // 获取 injected.dll 完整路径，推算 terminal_injector.exe 路径（同目录）
            wchar_t dllPath[MAX_PATH] = {0};
            DWORD len = GetModuleFileNameW(hSelf, dllPath, MAX_PATH);
            if (len > 0 && len < MAX_PATH) {
                // 去掉文件名，保留目录
                std::wstring exePath(dllPath);
                size_t pos = exePath.find_last_of(L"\\/");
                if (pos != std::wstring::npos) {
                    exePath = exePath.substr(0, pos + 1) + L"terminal_injector.exe";
                } else {
                    exePath = L"terminal_injector.exe";
                }

                // 构造命令行：--unload-remote <pid> 0x<dllBase>
                wchar_t cmdLine[MAX_PATH * 2];
                int n = swprintf_s(cmdLine, MAX_PATH * 2,
                    L"\"%ls\" --unload-remote %lu 0x%llx",
                    exePath.c_str(),
                    GetCurrentProcessId(),
                    static_cast<unsigned long long>(
                        reinterpret_cast<uintptr_t>(hSelf)));
                if (n > 0) {
                    // CreateProcessW 的 lpCommandLine 需可写缓冲
                    std::vector<wchar_t> cmdBuf(cmdLine, cmdLine + n + 1);
                    STARTUPINFOW si{};
                    si.cb = sizeof(si);
                    PROCESS_INFORMATION pi{};
                    // CREATE_NO_WINDOW：助手进程无窗口，不干扰用户
                    // DETACHED_PROCESS：与父进程控制台分离（避免 AttachConsole 冲突）
                    DWORD flags = CREATE_NO_WINDOW;
                    if (CreateProcessW(nullptr, cmdBuf.data(),
                                       nullptr, nullptr, FALSE,
                                       flags, nullptr, nullptr, &si, &pi)) {
                        LOG_INFO("Unload: helper process spawned, pid=%lu cmd=%ls",
                                 pi.dwProcessId, cmdLine);
                        // 不等待助手进程完成：DoUnload 线程需尽快 ExitThread
                        // 让 LDR 释放 ThreadBlob，助手进程的远程 FreeLibrary 才能成功
                        // 关闭句柄避免泄漏（进程对象由 OS 回收）
                        CloseHandle(pi.hThread);
                        CloseHandle(pi.hProcess);
                    } else {
                        LOG_ERROR("Unload: CreateProcessW(helper) failed err=%lu cmd=%ls",
                                  GetLastError(), cmdLine);
                    }
                } else {
                    LOG_ERROR("Unload: swprintf_s cmdLine failed");
                }
            } else {
                LOG_ERROR("Unload: GetModuleFileNameW failed err=%lu", GetLastError());
            }
        } else {
            LOG_ERROR("Unload: GetModuleHandleW(injected.dll) returned null");
        }
    }

    LOG_INFO("Unload complete, DLL ready for remote FreeLibrary");

    // 10. 关闭 Logger（必须在 ExitThread 之前）
    //     原因：RingBufferLogger::WorkerMain 线程在 injected.dll 代码中执行
    //     （阻塞在 SleepEx）。若不停止，LoadCount 无法归零，远程 FreeLibrary
    //     后 DETACH 不触发，形成循环依赖：
    //       FreeLibrary 等 LoadCount=0 → LoadCount 等 Logger 线程退出
    //       → Logger 线程退出等 Shutdown → Shutdown 在 DETACH 中调用
    //       → DETACH 等 FreeLibrary 触发
    //     Shutdown 内部设 m_shutdown=true + WaitForSingleObject 等 worker 退出，
    //     打破循环。Shutdown 后 LOG_INFO 不落盘（Logger 已关闭）。
    if (Logger::IsInitialized()) {
        LOG_INFO("Unload: shutting down logger before ExitThread");
        Logger::Shutdown();
    }

    // 11. 退出卸载线程（不调 FreeLibraryAndExitThread）
    //     卸载线程本身在 injected.dll 代码中执行，若不退出会持有 LdrpThreadBlob
    //     引用，远程 FreeLibrary 仍无法让 LoadCount 归零。ExitThread 让线程退出，
    //     LDR 释放 ThreadBlob，助手进程的远程 FreeLibrary 才能把 LoadCount 减到 0。
    ExitThread(0);
}

} // namespace terminjector
