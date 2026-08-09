// DLL 主动卸载器实现（Phase 11）
// 详见 docs/phases/11-unload-testing.md 4.2
//
// DoUnload 流程（顺序敏感，避免线程竞态）：
//   1. StatePoller.Stop       停后台轮询，避免轮询线程在 Hook 卸载后仍调 SendToMediator
//   2. BatchSender.Shutdown   flush 最后一批 VtOutput（需 transport 仍可用）
//   3. InputQueue.SignalDataReady  唤醒阻塞在 ReadConsoleInput 上的线程
//      （Hook 即将卸载，唤醒后下次调用走原 API，不再依赖 InputQueue）
//   4. HookManager.DisableAll   禁用所有 Hook（保留 trampoline）
//      之后 Console API 走原 API，不再进 Detour
//      （UninstallAll 会释放 trampoline，→ ReadDetour 线程 AV 崩溃）
//      DETACH 中 UninstallAll 做真正清理
//  4.5 等待 s_active_read_detours 归零（5s 超时强制继续）
//      Phase 22 修复：Read 类 Detour 阻塞型 pass-through 不再提前 release，
//      计数保持到 detour 函数真正返回；循环内每 300ms KickStartBlockedReaders
//      （向 ConHost 写回车）唤醒阻塞在原 Read* API 的线程，线程从 orig 返回后
//      走 detour 尾析构 guard → 计数归零 → 已彻底离开 DLL 代码，FreeLibrary 才安全
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
#include "state/VirtualConsoleState.h"
#include "state/VtReplayBuffer.h"
#include "state/PromptTracker.h"
#include "BatchSender.h"
#include "logging/Logger.h"
#include "protocol/Message.h"
#include "protocol/MessageSerializer.h"
#include "transport/ITransport.h"
#include "hooks/InputHooks.h"
#include "hooks/ProtectionHooks.h"

#include <windows.h>
#include <thread>
#include <cstdint>
#include <string>
#include <utility>
#include <vector>

namespace terminjector {

namespace {

// 获取可用于 console API 的输出句柄。
// 某些场景（cmd 批处理等待子进程等）下 GetStdHandle(STD_OUTPUT_HANDLE) 返回的
// 句柄对 console API 无效（GetConsoleScreenBufferInfo 报 err=6 ERROR_INVALID_HANDLE），
// 而 CONOUT$ 打开的总是当前进程真实控制台。优先用 std 句柄，失败回退 CONOUT$。
// 返回 (handle, shouldClose)：shouldClose=true 时调用方用完需 CloseHandle。
std::pair<HANDLE, bool> GetConsoleOutHandle() {
    HANDLE h = GetStdHandle(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO tmp{};
    if (h != nullptr && h != INVALID_HANDLE_VALUE &&
        GetConsoleScreenBufferInfo(h, &tmp)) {
        return {h, false};
    }
    HANDLE hCon = CreateFileW(L"CONOUT$", GENERIC_READ | GENERIC_WRITE,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                              OPEN_EXISTING, 0, nullptr);
    if (hCon != INVALID_HANDLE_VALUE) {
        return {hCon, true};
    }
    return {h, false};
}

} // namespace

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

    // 4. 禁用所有 Hook（保留 trampoline，不释放）
    //    之后目标程序调 Console API 直接走系统 API，不再进 Detour
    //    用 DisableAll 而非 UninstallAll：MH_RemoveHook 会释放 trampoline
    //    内存，若 ReadDetour 线程尚未完全退出，后续调用 *_orig 会 AV 崩溃
    //    （0xC0000005）。DLL_PROCESS_DETACH 中 UninstallAll 做最终清理。
    HookManager::DisableAll();

    // 4.5 先恢复 ConHost 画面（Phase 22 VT 重放），再唤醒 cmd 读取线程。
    //     竞态修复：若先唤醒 cmd（KickStart 回车）让它在重放前直接写新 prompt
    //     （hooks 已移除），重放内容会与 cmd 的 prompt 叠加 → 解除后双 prompt /
    //     拼行。先重放（此时 cmd 仍阻塞在读取，不干扰），重放后 ConHost 光标
    //     已推进到正确位置，再唤醒 cmd → cmd 的新 prompt 落在下一行。
    ReplaySessionToConHost();

    // 5. 恢复 ConsoleState 到真实 ConHost 状态
    //    Hook 已卸载，GetConsoleScreenBufferInfo 直接读 ConHost 真值
    //    写回 ConsoleState 缓存，让目标程序下次查询拿到 ConHost 真值
    const auto [hOut, hCloseOut] = GetConsoleOutHandle();
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
    if (hCloseOut) CloseHandle(hOut);

    // 5.5 等待所有 Read 类 Detour 线程离开 DLL 代码（Phase 22 卸载竞态修复）
    //     根因（cdb 取证 <Unloaded_injected.dll>+0x97a1 Access violation）：
    //       线程在真实 ReadConsoleW 返回后经 trampoline 尾跳回 detour 主体时，
    //       DLL 已被远程 FreeLibrary → AV。旧方案在 pass-through 调 orig 前
    //       LeaveReadDetour（提前减计数），Unloader 看到 count==0 时线程仍在
    //       orig 内部 → 等待失效。
    //     修复：Read 类 Detour 阻塞型 pass-through 不再提前 release，计数保持
    //       到 detour 函数真正返回（guard 析构）才归零；此处（DisableAll 后）
    //       反复 KickStart 向 ConHost 写回车，唤醒阻塞在原 API 的线程返回，
    //       线程走完 detour 尾（析构 guard）→ 计数归零 → 线程已完全离开 DLL 代码，
    //       之后 FreeLibrary 才安全。
    //     超时兜底防死锁：极端情况（如阻塞在重定向管道读）仍要继续卸载。
    //     （本步在重放之后：cmd 被回车唤醒后写新 prompt 时，重放已完成，
    //       ConHost 光标已推进到正确位置，新 prompt 落在下一行，不再拼行。）
    {
        constexpr int kWaitTotalMs = 5000;
        constexpr int kSleepStepMs = 10;
        constexpr int kKickEveryMs = 300;
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
            // 周期性踢 ConHost：唤醒阻塞在原 ReadConsoleW/ReadFile/ReadConsoleInput
            // 的线程（它们从 orig 返回后走 detour 尾析构 guard，计数归零）
            if ((waited % kKickEveryMs) < kSleepStepMs) {
                hooks::KickStartBlockedReaders();
            }
        }
        if (ActiveReadDetours() != 0) {
            LOG_WARN("Unload: %d read detour(s) still active after %dms, force continue "
                     "(trampoline kept by DisableAll, FreeLibrary may still AV if thread blocked)",
                     ActiveReadDetours(), kWaitTotalMs);
        }
    }

    // 6. 显示原 Console 窗口（Phase 9 隐藏过，恢复可见让用户能继续操作）
    //    GetConsoleWindow 已 Hook 返回 NULL，走 orig 拿真实 HWND
    HWND hCon = hooks::CallRealGetConsoleWindow();
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

    // 8. 停止接收线程（置 g_recvRunning=false）
    //    Phase 22 修复：原实现只在 DLL_PROCESS_DETACH 停 DllRecvLoop 线程，
    //    而 DETACH 需远程 FreeLibrary 成功才触发 → 循环依赖：线程持续执行
    //    DLL 代码 → LDR 延迟卸载（LdrModulesReadyToUnload）→ 卸载在线程
    //    Sleep 时完成 → 线程醒来继续执行已释放的 DLL 代码 → Execute AV
    //    （cdb 取证：<Unloaded_injected.dll>+0x97a1 RecvLoopMain 循环内 jmp，
    //    procdump 确认未处理异常 0xC0000005）。此处主动停线程，打破依赖。
    StopDllRecvLoop();

    // 8.5 释放 mediator 传输通道（断开命名管道）
    //    之后 SendToMediator 调用会因 transport=nullptr 直接返回 false；
    //    DllRecvLoop 线程下次循环 GetMediatorTransport() 返回 nullptr → 退出；
    //    若线程正阻塞在 RecvPacket(ReadFile)，管道断开后立即失败返回
    ReleaseMediatorTransport();

    // 8.6 等待接收线程真正退出
    //    线程已不在 DLL 代码中，远程 FreeLibrary 才能顺利让 LoadCount 归零
    //    （无残留 ThreadBlob 引用，不再需要 LDR flush 强制卸载）
    JoinDllRecvLoop();

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

// 恢复 ConHost 画面为 WT 会话画面（Phase 22）
// 详见 docs/phases/22-conhost-replay.md
//
// 流程（全部走真实 API，Hook 已在 DoUnload 步骤 4 禁用）：
//   1. 将 ConHost 缓冲 resize 到会话最终尺寸（先缩视口再缩缓冲，避免 API 失败）
//   2. 视口定位到会话窗口区域（尽力而为，失败仅记日志）
//   3. 开启 ConHost 的 ENABLE_VIRTUAL_TERMINAL_PROCESSING
//   4. 用 WriteFile 分块重放会话 VT 流
//      （翻译器输出 UTF-8 字节，重放前切输出代码页为 UTF-8，
//        否则 ConHost(如 GBK/936) 会把 UTF-8 多字节按本地代码页解码 → 中文乱码；
//        重放结束后恢复原代码页）
//   5. 恢复 ConHost 输出模式
//
// 不使用 WriteConsoleW：会把 UTF-8 字节当 UTF-16 宽字符写入，中文会乱码
// 重放前不清屏：ConHost 旧画面即注入时快照，增量流在快照之上重放
void Unloader::ReplaySessionToConHost() {
    const std::string& vt = VtReplayBuffer::Instance().Data();
    if (vt.empty()) {
        LOG_INFO("Replay: no session VT recorded, skip ConHost replay");
        return;
    }

    // std 句柄可能对 console API 无效（cmd 批处理等场景），回退 CONOUT$；
    // 整个重放过程（resize/设窗/开 VT/WriteFile）都用该可靠句柄
    const auto [hOut, hCloseOut] = GetConsoleOutHandle();
    if (hOut == nullptr || hOut == INVALID_HANDLE_VALUE) {
        LOG_WARN("Replay: no usable console output handle, skip ConHost replay");
        return;
    }

    // 会话最终尺寸（虚拟状态，由 Set* Hook 与 WT resize 维护）
    const COORD targetBuf = VirtualConsoleState::Instance().GetBufferSize();
    const SMALL_RECT targetWin = VirtualConsoleState::Instance().GetWindowRect();

    CONSOLE_SCREEN_BUFFER_INFO cur{};
    if (!GetConsoleScreenBufferInfo(hOut, &cur)) {
        LOG_WARN("Replay: GetConsoleScreenBufferInfo failed err=%lu", GetLastError());
        return;
    }
    LOG_INFO("Replay: session buf=(%d,%d) win=(%d,%d,%d,%d), cur buf=(%d,%d), vt=%zu bytes%s",
             targetBuf.X, targetBuf.Y, targetWin.Left, targetWin.Top,
             targetWin.Right, targetWin.Bottom, cur.dwSize.X, cur.dwSize.Y,
             vt.size(), VtReplayBuffer::Instance().IsTruncated() ? " (truncated)" : "");

    // 1. resize 缓冲到会话尺寸 —— 只放大,绝不缩小。
    //    缩小(如从 9001 行压到视口 32 行)会丢弃 ConHost 缓冲中的滚动历史
    //    scrollback(用户反馈:卸载重放后 ConHost 没有历史)。
    //    背景:WT resize 会把 VirtualConsoleState 的虚拟缓冲压到视口高
    //    (ApplyWtResize 中 bufferSize = max(rows, rows+scrollbackLines),
    //    而 scrollbackLines 在 9001 行缓冲下几乎不会触发),卸载时若照此
    //    SetConsoleScreenBufferSize 缩小,缓冲顶部历史整段丢失。
    //    ConHost 缓冲在注入时冻结,已保留全部历史(滚动区+可见区);
    //    回放把会话增量写在快照之上,保持原尺寸(必要时增大)即可容纳全部内容。
    if (targetBuf.X > cur.dwSize.X || targetBuf.Y > cur.dwSize.Y) {
        COORD newBuf;
        newBuf.X = (targetBuf.X > cur.dwSize.X) ? targetBuf.X : cur.dwSize.X;
        newBuf.Y = (targetBuf.Y > cur.dwSize.Y) ? targetBuf.Y : cur.dwSize.Y;
        if (!SetConsoleScreenBufferSize(hOut, newBuf)) {
            LOG_WARN("Replay: SetConsoleScreenBufferSize(%d,%d) failed err=%lu "
                     "(keep current buffer, replay may clip)",
                     newBuf.X, newBuf.Y, GetLastError());
        }
    }

    // 2. 视口定位到会话窗口区域（尺寸超 ConHost 最大窗口时失败，保持当前视口）
    //    重要：不能直接用 targetWin（VirtualConsoleState 的窗口被 LazyInit
    //    Phase 14 用 WT 尺寸覆盖成顶部锚定 [0..rows-1]）。重放 VT 的光标同步是
    //    视口相对坐标（termCursorY = 注入时 cursor.Y - srWindow.Top），行号基准
    //    是【注入时】的 srWindow.Top。窗口顶部必须保持该值，行号才映射到正确
    //    绝对行。尺寸用会话最终尺寸。
    {
        const SMALL_RECT injWin = VirtualConsoleState::Instance().GetInjectionWindow();
        SMALL_RECT replayWin;
        replayWin.Left   = injWin.Left;
        replayWin.Top    = injWin.Top;
        replayWin.Right  = static_cast<SHORT>(
            (targetWin.Right - targetWin.Left) + replayWin.Left);
        replayWin.Bottom = static_cast<SHORT>(
            (targetWin.Bottom - targetWin.Top) + replayWin.Top);
        if (!SetConsoleWindowInfo(hOut, TRUE, &replayWin)) {
            LOG_WARN("Replay: SetConsoleWindowInfo(%d,%d)-(%d,%d) failed err=%lu",
                     replayWin.Left, replayWin.Top,
                     replayWin.Right, replayWin.Bottom, GetLastError());
        }
    }

    // 3. 开启 VT 处理（ConHost 原生支持，重放会话 VT 流）
    DWORD origMode = 0;
    GetConsoleMode(hOut, &origMode);

    // 3.1 重放前把光标移回窗口内（prompt 行行首）。会话期 cmd 交互（KickStart
    //     回车等）可能把 ConHost 光标移到窗口下方（如 (0,92) vs 窗口 [61..90]），
    //     此时重放 VT 的 CUP 视口相对定位会被 ConHost 异常解释。光标归位到
    //     (0, 注入光标行)，即使后续 CUP 失效，文本也落在行首覆盖旧 prompt。
    //     同时记录重放前光标：重放后若光标未动（惰性重放 = 会话无可视内容），
    //     5.5 据此把光标抬到擦除行上一行，让 KickStart 回显的 \r\n 把 cmd
    //     新 prompt 推回注入前原位（无多余空行）。
    //
    // 注意：preReplayCur 必须记录【归位后】的光标。此前记录归位前的值
    // （KickStart 回车回显 \r\n 后的 (0,N+1)），而空会话重放只有 SGR
    // （\x1b[0m，不移动光标），重放后光标 (0,N) 与记录值 (0,N+1) 不等，
    // 惰性分支永不触发 → 每次卸载 prompt 下移一行、快照 prompt 行留空，
    // 注入/卸载循环后统计行与 prompt 间的空行累积（用户反馈 BUG）。
    bool hasPreCur = false;
    COORD preReplayCur{0, 0};
    {
        CONSOLE_SCREEN_BUFFER_INFO now{};
        if (GetConsoleScreenBufferInfo(hOut, &now)) {
            if (now.dwCursorPosition.Y > now.srWindow.Bottom ||
                now.dwCursorPosition.Y < now.srWindow.Top ||
                now.dwCursorPosition.X < now.srWindow.Left ||
                now.dwCursorPosition.X > now.srWindow.Right) {
                const COORD injCur = VirtualConsoleState::Instance().GetInjectionCursor();
                COORD home;
                home.X = 0;
                home.Y = injCur.Y;
                if (SetConsoleCursorPosition(hOut, home)) {
                    LOG_INFO("Replay: cursor outside window (%d,%d), moved to line start (%d,%d)",
                             now.dwCursorPosition.X, now.dwCursorPosition.Y,
                             home.X, home.Y);
                    // 归位成功后以新位置作为重放前基准
                    now.dwCursorPosition = home;
                } else {
                    LOG_WARN("Replay: SetConsoleCursorPosition(%d,%d) failed err=%lu",
                             home.X, home.Y, GetLastError());
                }
            }
            // 归位之后记录重放前光标（见 3.1 注释：惰性重放判据基准）
            hasPreCur = true;
            preReplayCur = now.dwCursorPosition;
        }
    }

    if (!SetConsoleMode(hOut, origMode | ENABLE_VIRTUAL_TERMINAL_PROCESSING)) {
        LOG_WARN("Replay: SetConsoleMode(VT) failed err=%lu", GetLastError());
        return;  // 无 VT 处理能力则无法重放，跳过
    }

    // 3.5 切换输出代码页到 UTF-8
    // 会话 VT 流是 UTF-8 字节；不切换则 ConHost 按本地代码页（如 GBK/936）
    // 解码多字节字符 → 中文乱码。重放结束后恢复原代码页
    UINT origCp = GetConsoleOutputCP();
    if (origCp != CP_UTF8) {
        if (SetConsoleOutputCP(CP_UTF8)) {
            LOG_INFO("Replay: output codepage switched %u -> 65001(UTF-8)", origCp);
        } else {
            LOG_WARN("Replay: SetConsoleOutputCP(65001) failed err=%lu, "
                     "replay may show mojibake for CJK", GetLastError());
        }
    } else {
        LOG_INFO("Replay: output codepage already UTF-8");
    }

    // 4. 确定重放终点：行编辑 shell 停在 prompt 时，截断到最后 prompt 起点
    //     写-读序列语义（PromptTracker）：行编辑 shell 的循环是"画 prompt →
    //     阻塞 ReadConsole"，prompt = "行编辑读入口之前的最后一次输出写入"，
    //     不需要猜内容是否以 > 结尾。
    //     截断效果：该 prompt 不进 ConHost（重放终点 = prompt 写入起点），
    //     快照里的旧 prompt 行由 4.1 在重放前擦除，shell 被唤醒后重绘的新
    //     prompt 成为唯一 → 单 prompt 无缝；旧启发式"擦除 prompt 样末行"
    //     因此移除。
    //     仅当行编辑读当前活动（shell 正停在 prompt 等输入）时截断：
    //       命令执行中卸载（长输出如 tree /f）不截断，避免丢输出；
    //       TUI 全屏程序（无 ECHO_INPUT）不记录，永不截断。
    const auto promptOff = PromptTracker::Instance().TruncateOffset();
    size_t replayEnd = vt.size();
    if (promptOff.has_value()) {
        replayEnd = *promptOff;
        LOG_INFO("Replay: truncating at line-shell prompt offset %zu, "
                 "replay %zu/%zu bytes", *promptOff, replayEnd, vt.size());
    } else {
        LOG_INFO("Replay: no active line-shell prompt, replay full %zu bytes",
                 vt.size());
    }

    // 4.1 擦掉快照 prompt 行（截断命中时，重放前）。
    //     截断 = 重放终点为最后 prompt 写入起点，重放内容【不含】该 prompt
    //     文本——重放只在当前光标处写 vt[0,replayEnd)，快照里的旧 prompt
    //     （注入光标行，行编辑 shell 停在 prompt 等输入时 injCur.Y 即 prompt
    //     行）永不会被重放覆盖；cmd 唤醒后重绘的新 prompt 又落在其后一行
    //     （KickStart 回车回显 \r\n 推下行）→ 空会话反复注入/卸载会累积双
    //     prompt。重放前确定性擦掉该行（非内容启发式），cmd 的新 prompt
    //     成为唯一；若重放内容恰要写该行，也会直接覆盖擦除后的空白。
    //     重放终点 = 0（会话只有 prompt）时同样适用。
    if (promptOff.has_value()) {
        CONSOLE_SCREEN_BUFFER_INFO now{};
        if (GetConsoleScreenBufferInfo(hOut, &now)) {
            COORD rowStart;
            rowStart.X = 0;
            rowStart.Y = VirtualConsoleState::Instance().GetInjectionCursor().Y;
            if (rowStart.Y >= 0 && rowStart.Y < now.dwSize.Y) {
                DWORD filled = 0;
                FillConsoleOutputCharacterW(hOut, L' ', now.dwSize.X, rowStart, &filled);
                FillConsoleOutputAttribute(hOut, now.wAttributes,
                                           now.dwSize.X, rowStart, &filled);
                LOG_INFO("Replay: truncated at prompt, erased snapshot prompt row %d "
                         "(replayEnd=%zu)", rowStart.Y, replayEnd);
            }
        }
    }

    // 4.2 分块重放（WriteFile 字节流，ConHost VT 模式按 UTF-8 解析）
    constexpr DWORD kChunkBytes = 64 * 1024;
    size_t offset = 0;
    while (offset < replayEnd) {
        DWORD n = static_cast<DWORD>(
            (kChunkBytes < replayEnd - offset) ? kChunkBytes : (replayEnd - offset));
        DWORD written = 0;
        if (!WriteFile(hOut, vt.data() + offset, n, &written, nullptr) || written == 0) {
            LOG_WARN("Replay: WriteFile failed err=%lu at offset=%zu", GetLastError(), offset);
            break;
        }
        offset += written;
    }
    LOG_INFO("Replay: replayed %zu/%zu VT bytes to ConHost", offset, replayEnd);

    // 5. 恢复 ConHost 输出模式（含 VT 位原状）
    SetConsoleMode(hOut, origMode);

    // 5.5 重放后光标处理。
    //     已截断（行编辑 shell 停在 prompt）：重放终点 = prompt 写入起点，
    //     cursor 停在重放内容末尾（行编辑读入口）；不推进——cmd 唤醒后
    //     KickStart 回车回显 \r\n 会自然把新 prompt 推到下一行，快照旧
    //     prompt 行已在 4.1 擦除，单 prompt 成立。
    //       惰性重放（重放前后光标未动 = 会话无可视内容，空会话）：光标
    //       抬到擦除行上一行，回显 \r\n 恰好把 cmd 新 prompt 推回注入前
    //       原位（prompt 行），画面与注入前逐像素一致，无多余空行。
    //     未截断（命令执行中卸载 / TUI 全屏）：光标停在会话末输出末尾，推进
    //     到下一行行首，cmd 处理 KickStart 队列里的回车后写新 prompt 时落新行，
    //     避免拼到输出末行（用户反馈：解除后 prompt 拼到输出末行
    //     "或批处理文件。>"）。
    if (!promptOff.has_value()) {
        CONSOLE_SCREEN_BUFFER_INFO end{};
        if (GetConsoleScreenBufferInfo(hOut, &end)) {
            COORD next;
            next.X = 0;
            next.Y = static_cast<SHORT>(end.dwCursorPosition.Y + 1);
            if (next.Y >= end.dwSize.Y) next.Y = static_cast<SHORT>(end.dwSize.Y - 1);
            SetConsoleCursorPosition(hOut, next);
        }
    } else if (hasPreCur) {
        // 已截断 + 惰性重放：光标抬到擦除行上一行（见 5.5 注释）
        CONSOLE_SCREEN_BUFFER_INFO end{};
        if (GetConsoleScreenBufferInfo(hOut, &end) &&
            end.dwCursorPosition.X == preReplayCur.X &&
            end.dwCursorPosition.Y == preReplayCur.Y) {
            const COORD injCur = VirtualConsoleState::Instance().GetInjectionCursor();
            COORD target;
            target.X = 0;
            target.Y = static_cast<SHORT>(injCur.Y - 1);
            if (target.Y < 0) target.Y = 0;
            if (SetConsoleCursorPosition(hOut, target)) {
                LOG_INFO("Replay: inert replay, cursor raised to row %d (echo \\r\\n "
                         "will put shell prompt back at injection row %d)",
                         target.Y, injCur.Y);
            } else {
                LOG_WARN("Replay: SetConsoleCursorPosition(%d,%d) failed err=%lu",
                         target.X, target.Y, GetLastError());
            }
        }
    }

    // 6. 恢复 ConHost 输出代码页（先关 VT 再改代码页，还原原始解码规则）
    if (origCp != CP_UTF8 && origCp != 0) {
        SetConsoleOutputCP(origCp);
        LOG_INFO("Replay: output codepage restored %u", origCp);
    }

    // 回退路径打开的 CONOUT$ 句柄用完即关（std 句柄路径 hCloseOut=false）
    if (hCloseOut) CloseHandle(hOut);
}

} // namespace terminjector
