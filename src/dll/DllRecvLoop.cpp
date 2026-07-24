// DllRecvLoop 实现：DLL 侧后台接收线程
// 详见 docs/phases/05-cursor-buffer.md 4.6
//
// 关键点：
//   - 复用 LazyInit 建立的 g_transport（与 Send 共享同一管道，全双工）
//   - 使用 Peek 轮询而非阻塞 RecvPacket，避免阻塞 ReadFile 锁住同步管道句柄
//   - 非 overlapped 管道句柄在 I/O 期间持锁（MSDN: 一次只能有一个 I/O 操作），
//     阻塞的 ReadFile 会阻止主线程 WriteFile 发送 VtOutput → 死锁
//   - Peek 非阻塞检查有无数据，有数据才调 RecvPacket（此时 ReadFile 不会长阻塞）
//   - pipe 断开时 Peek 返回 -1，线程退出
//
// 注意：transport 的 Send/Recv 可并发（NamedPipeTransport::Send 已加锁）
#include "DllRecvLoop.h"
#include "LazyInit.h"
#include "transport/ITransport.h"
#include "protocol/MessageSerializer.h"
#include "protocol/Message.h"
#include "state/ConsoleState.h"
#include "state/InputQueue.h"          // Phase 6：输入队列
#include "translator/VtInputParser.h"  // Phase 6：VT 输入分帧
#include "hooks/SignalHooks.h"         // Phase 7：Ctrl+C 触发
#include "logging/Logger.h"

#include <windows.h>
#include <thread>
#include <atomic>
#include <vector>
#include <cstring>

namespace terminjector {

namespace {

std::thread       g_recvThread;
std::atomic<bool> g_recvRunning{false};

// 接收循环主体
// 使用 Peek 轮询模式：先非阻塞检查管道有无数据，有数据才调 RecvPacket
// 避免阻塞 ReadFile 持锁阻止主线程 WriteFile（同步管道句柄一次只能一个 I/O）
void RecvLoopMain() {
    ITransport* transport = GetMediatorTransport();
    if (transport == nullptr) {
        LOG_WARN("DllRecvLoop: transport is null, exit");
        return;
    }

    LOG_INFO("DllRecvLoop started");
    // Phase 6：VT 输入分帧解析器（局部变量，跨 while 循环保持半序列缓冲状态）
    VtInputParser vtInputParser;
    uint8_t peekBuf[1];  // Peek 探测缓冲区（只需 1 字节判断有无数据）
    int peekCount = 0;   // Phase 6 诊断：Peek 次数采样

    // ESC 超时交付：单独 ESC(0x1b) 被 VtInputParser 当作不完整序列缓冲
    // 等 50ms 无新数据后通过 FlushPending 交付为 VK_ESCAPE
    // 0 表示无悬挂 ESC，非 0 为 GetTickCount() 时间戳
    DWORD pendingEscTick = 0;
    const DWORD ESC_TIMEOUT_MS = 50;

    while (g_recvRunning.load()) {
        // 非阻塞 Peek：检查管道内是否有数据可读
        int peeked = transport->Peek(peekBuf, 1);
        // Phase 6 诊断：前 5 次 Peek 打印返回值，确认线程在运行
        if (peekCount < 5) {
            LOG_INFO("DllRecvLoop: Peek #%d returned %d", peekCount, peeked);
            peekCount++;
        }
        if (peeked < 0) {
            // 管道出错或断开，退出循环
            LOG_INFO("DllRecvLoop: pipe error/broken (Peek=%d)", peeked);
            break;
        }
        if (peeked == 0) {
            // Phase 10 任务2：检查鼠标攒批超时，flush 到主队列
            // EnqueueBatched 超时检查依赖下次调用，无新鼠标事件时 m_batch 会卡住，
            // 此处补全：peek=0 表示空闲，是检查超时的时机（最多 16ms+10ms≈26ms 延迟）
            InputQueue::Instance().FlushBatchTimeout();

            // 无数据：检查是否有悬挂 ESC 需要超时交付
            if (pendingEscTick > 0) {
                DWORD elapsed = GetTickCount() - pendingEscTick;
                if (elapsed >= ESC_TIMEOUT_MS) {
                    auto records = vtInputParser.FlushPending();
                    if (!records.empty()) {
                        InputQueue::Instance().EnqueueRecords(records.data(),
                                                              records.size());
                        LOG_DEBUG("DllRecvLoop: ESC flushed after %lu ms",
                                  elapsed);
                    }
                    pendingEscTick = 0;
                }
            }
            // 短暂休眠后重试（10ms 轮询，ResizeNotify 延迟可忽略）
            Sleep(10);
            continue;
        }

        // 有数据可读，调 RecvPacket 读取完整包
        // 此时管道内有数据，ReadFile 不会长时间阻塞，不影响主线程 WriteFile
        protocol::MessageType type;
        std::vector<uint8_t> payload;
        if (!protocol::RecvPacket(transport, type, payload)) {
            LOG_INFO("DllRecvLoop: pipe closed (RecvPacket failed)");
            break;
        }

        switch (type) {
            case protocol::MessageType::ResizeNotify: {
                // WT 窗口尺寸变化：更新 ConsoleState 的 dwSize 与 srWindow
                if (payload.size() < sizeof(protocol::ResizePayload)) {
                    LOG_WARN("DllRecvLoop: ResizeNotify payload too small %zu",
                             payload.size());
                    break;
                }
                protocol::ResizePayload p{};
                std::memcpy(&p, payload.data(), sizeof(p));

                // 更新缓冲区尺寸
                COORD bufSize;
                bufSize.X = static_cast<SHORT>(p.bufferCols);
                bufSize.Y = static_cast<SHORT>(p.bufferRows);
                ConsoleState::Instance().SetBufferSize(bufSize);

                // 更新窗口矩形（左上角为 0,0，右下角为 cols-1,rows-1）
                SMALL_RECT win;
                win.Left   = 0;
                win.Top    = 0;
                win.Right  = static_cast<SHORT>(p.cols - 1);
                win.Bottom = static_cast<SHORT>(p.rows - 1);
                ConsoleState::Instance().SetWindow(win);

                LOG_INFO("DllRecvLoop: Resize applied win=%ux%u buf=%ux%u",
                         p.cols, p.rows, p.bufferCols, p.bufferRows);
                break;
            }
            case protocol::MessageType::VtInput: {
                // Phase 6：根据输入模式选择透传或翻译
                auto& state = ConsoleState::Instance();
                DWORD inputMode = state.GetInputMode();
                if (inputMode & ENABLE_VIRTUAL_TERMINAL_INPUT) {
                    // 透传模式：原始 VT 字节直接入 raw 队列
                    // vim/less 等程序开 VT 输入模式，期望收到原始 VT 字节
                    InputQueue::Instance().EnqueueRaw(payload.data(), payload.size());
                    pendingEscTick = 0;  // 透传模式不用 ESC 超时
                } else {
                    // 翻译模式：VT 序列 → INPUT_RECORD → 入 record 队列
                    // cmd 等传统程序用 ReadConsoleInput 读结构化事件
                    auto records = vtInputParser.Feed(payload.data(), payload.size());
                    if (!records.empty()) {
                        // Phase 7：检测 Ctrl+C（\x03），触发 CtrlHandler
                        // 在入队前触发，让程序注册的回调先处理
                        for (const auto& r : records) {
                            if (r.EventType == KEY_EVENT &&
                                r.Event.KeyEvent.bKeyDown &&
                                r.Event.KeyEvent.uChar.UnicodeChar == 0x03) {
                                LOG_INFO("DllRecvLoop: Ctrl+C detected, triggering handlers");
                                hooks::TriggerCtrlC();
                                break;  // 一次只触发一次
                            }
                        }
                        // Phase 10 任务2：键盘即时入队（低延迟），鼠标攒批入队（减锁竞争）
                        // 鼠标高频移动产生大量 MOUSE_EVENT，攒批 16ms/20 条一次入队
                        std::vector<INPUT_RECORD> keyRecs, mouseRecs;
                        for (const auto& r : records) {
                            if (r.EventType == MOUSE_EVENT) {
                                mouseRecs.push_back(r);
                            } else {
                                keyRecs.push_back(r);
                            }
                        }
                        if (!keyRecs.empty()) {
                            InputQueue::Instance().EnqueueRecords(keyRecs.data(),
                                                                  keyRecs.size());
                        }
                        if (!mouseRecs.empty()) {
                            InputQueue::Instance().EnqueueBatched(mouseRecs.data(),
                                                                  mouseRecs.size());
                        }
                        pendingEscTick = 0;  // 有完整序列产出，清除悬挂标记
                    } else if (vtInputParser.HasPending()) {
                        // Feed 返回空但有悬挂字节（如单独 ESC）
                        // 记录时间戳，等 50ms 无新数据后调 FlushPending 交付
                        pendingEscTick = GetTickCount();
                    }
                }
                LOG_DEBUG("DllRecvLoop: VtInput processed, len=%zu mode=0x%x",
                          payload.size(), inputMode);
                break;
            }
            case protocol::MessageType::Ping:
                // Phase 10 实现：回 Pong
                LOG_DEBUG("DllRecvLoop: Ping ignored (Phase 10)");
                break;
            case protocol::MessageType::Shutdown:
                // Phase 11 实现：卸载 Hook
                LOG_DEBUG("DllRecvLoop: Shutdown ignored (Phase 11)");
                break;
            case protocol::MessageType::ChildExitSync: {
                // 子进程退出后，mediator 同步 ConPTY 当前光标到此父进程 DLL
                // 用 ConPTY 光标覆盖 ConsoleState 缓存，使后续输出（如 cmd 新 prompt）
                // 接在子进程输出之后，而非覆盖子进程输出
                if (payload.size() < sizeof(protocol::ChildExitSyncPayload)) {
                    LOG_WARN("DllRecvLoop: ChildExitSync payload too small %zu",
                             payload.size());
                    break;
                }
                protocol::ChildExitSyncPayload sync{};
                std::memcpy(&sync, payload.data(), sizeof(sync));
                COORD c;
                c.X = static_cast<SHORT>(sync.cursorX);
                c.Y = static_cast<SHORT>(sync.cursorY);
                ConsoleState::Instance().SetCursorPosition(c);
                LOG_INFO("DllRecvLoop: ChildExitSync cursor synced to (%u,%u)",
                         sync.cursorX, sync.cursorY);
                break;
            }
            default:
                LOG_DEBUG("DllRecvLoop: unknown msg type=0x%08X len=%zu",
                          static_cast<uint32_t>(type), payload.size());
                break;
        }
    }
    LOG_INFO("DllRecvLoop exit");
}

} // namespace

void StartDllRecvLoop() {
    if (g_recvRunning.load()) return;  // 已在运行
    g_recvRunning.store(true);
    g_recvThread = std::thread(RecvLoopMain);
}

void StopDllRecvLoop() {
    if (!g_recvRunning.load()) return;
    g_recvRunning.store(false);
    // 轮询模式下线程在 Peek/Sleep 循环中，g_recvRunning=false 后最多 10ms 退出
    // 注意：ReleaseMediatorTransport 在 DLL_PROCESS_DETACH 才调用
    if (g_recvThread.joinable()) {
        // 不 join：避免 DLL_PROCESS_DETACH 中卡死（Loader Lock）
        // 线程会在下次 while 检查 g_recvRunning 时自然退出
        g_recvThread.detach();
    }
}

} // namespace terminjector
