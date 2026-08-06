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
#include "Unloader.h"                  // Phase 11：管道断开触发主动卸载
#include "transport/ITransport.h"
#include "protocol/MessageSerializer.h"
#include "protocol/Message.h"
#include "state/ConsoleState.h"
#include "state/VirtualConsoleState.h"
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
            // 管道出错或断开，退出循环并触发主动卸载（Phase 11）
            // 之前只 break 退出循环，DLL 仍驻留内存、Hook 仍激活
            // 现在调 Unloader::RequestUnload 在独立线程执行 FreeLibraryAndExitThread
            LOG_INFO("DllRecvLoop: pipe error/broken (Peek=%d), requesting unload", peeked);
            Unloader::RequestUnload();
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
            // 管道关闭（对端 mediator 退出），触发主动卸载（Phase 11）
            LOG_INFO("DllRecvLoop: pipe closed (RecvPacket failed), requesting unload");
            Unloader::RequestUnload();
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

                // 注入 WINDOW_BUFFER_SIZE_EVENT 通知等待 ReadConsoleInput 的程序
                // （如 Textual）窗口尺寸已变化，需要重新查询 GetConsoleScreenBufferInfo
                // 并刷新布局。若不注入，TUI 程序永远不会收到 resize 通知，
                // 在 WT 尺寸变化后不会重绘，导致布局与窗口不匹配。
                // 注：真实 ConHost/ConPTY 中 WINDOW_BUFFER_SIZE_EVENT 不受
                // ENABLE_WINDOW_INPUT 门控——只要 buffer/window 尺寸变化即产生
                // （见 microsoft/terminal#263 的官方确认，view 变化即发送该事件）。
                // Textual 的 enable_application_mode() 只设 ENABLE_VIRTUAL_TERMINAL_INPUT，
                // 若此处按 ENABLE_WINDOW_INPUT 过滤，事件将被吞掉、TUI 永不 resize。
                // 若仅在读口过滤：GetNumberOfConsoleInputEvents（未过滤计数）会
                // 报告队列有事件，但 ReadConsoleInputW 读不到，程序阻塞空等。
                // 故这里无条件注入该事件；不读的程序从 ReadConsoleInputW 读到后
                // 自行调用 GetConsoleScreenBufferInfo 核对，尺寸未变则无害。
                InputQueue::Instance().EnqueueResizeEvent(
                    static_cast<SHORT>(p.cols),
                    static_cast<SHORT>(p.rows));

                // 修复：子进程（python 等）只收 ResizeNotify，与主进程的
                // WtStateReport resize 路径不同步 VirtualConsoleState，
                // 导致子进程的 bufferSize/scrollback 不随 WT resize 更新。
                // 这里与主进程 ApplyWtResize 对齐，保持两状态一致。
                VirtualConsoleState::Instance().ApplyWtResize(p.cols, p.rows);

                LOG_INFO("DllRecvLoop: Resize applied win=%ux%u buf=%ux%u, EnqueueResizeEvent injected",
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
                    // 鼠标 SGR（CSI < b ; c ; r M/m）按 VT 流分帧，逐字节展开为
                    // KEY_EVENT 序列入 record 队列，供字节流消费者使用：
                    //   - mimo/Bun（libuv raw 读取器）只消费 KEY_EVENT，把每个
                    //     KEY_EVENT.UnicodeChar 还原为原始字节喂给 process.stdin，
                    //     OpenTUI 由这些字节解析 SGR 鼠标序列（0x1b[<b;c;rM）。
                    //     若交付 MOUSE_EVENT，libuv 丢弃（非 KEY_EVENT 忽略），
                    //     鼠标永不生效（根因，2026-08-04 实测确认）。
                    //   - 键盘/普通字符序列：仍走 VtToInputRecord::Parse 翻译为
                    //     结构化 KEY_EVENT，与工具模式一致。
                    // 说明（历史）：早期把整个 SGR 汇成单条 MOUSE_EVENT 交付，适合
                    // 读 ReadConsoleInputW 的 TUI；mimo 读取器逐字节还原则必须保留
                    // 原始 SGR 字节，故此处逐字节展开而非结构化交付。
                    auto rawSeqs = vtInputParser.FrameRaw(payload.data(),
                                                          payload.size());
                    std::vector<INPUT_RECORD> keyRecs;
                    for (const auto& seq : rawSeqs) {
                        bool isMouse = seq.size() >= 3 &&
                                       static_cast<uint8_t>(seq[0]) == 0x1b &&
                                       seq[1] == '[' && seq[2] == '<';
                        if (isMouse) {
                            // SGR 鼠标序列：逐字节展开为 KEY_EVENT（字符按键）
                            // 每个 UnicodeChar 即该字节值，bKeyDown 置 TRUE，
                            // libuv 会将每个字符转换回字节并拼接为原始 SGR 流
                            for (uint8_t b : seq) {
                                INPUT_RECORD rec{};
                                rec.EventType = KEY_EVENT;
                                rec.Event.KeyEvent.bKeyDown = TRUE;
                                rec.Event.KeyEvent.wRepeatCount = 1;
                                rec.Event.KeyEvent.uChar.UnicodeChar =
                                    static_cast<wchar_t>(b);
                                rec.Event.KeyEvent.dwControlKeyState = 0;
                                keyRecs.push_back(rec);
                            }
                        } else {
                            // 键盘序列：结构化 KEY_EVENT 翻译
                            auto recs = VtToInputRecord::Parse(
                                reinterpret_cast<const uint8_t*>(seq.data()),
                                seq.size());
                            keyRecs.insert(keyRecs.end(), recs.begin(), recs.end());
                        }
                    }
                    if (!keyRecs.empty()) {
                        InputQueue::Instance().EnqueueRecords(keyRecs.data(),
                                                              keyRecs.size());
                    }
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
                // Phase 11：mediator 主动要求卸载（如用户关闭 WT tab）
                // 调 Unloader::RequestUnload 触发 FreeLibraryAndExitThread
                // RequestUnload 幂等，多次调用只首次执行
                LOG_INFO("DllRecvLoop: Shutdown received, requesting unload");
                Unloader::RequestUnload();
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
                VirtualConsoleState::Instance().SetCursorPos(c);
                LOG_INFO("DllRecvLoop: ChildExitSync cursor synced to (%u,%u)",
                         sync.cursorX, sync.cursorY);
                break;
            }
            case protocol::MessageType::WtStateReport: {
                // Phase 14：WT 状态反向同步
                // 中介报告 WT 侧真实状态（resize 或 DSR CPR 响应）
                // 更新 VirtualConsoleState 使程序查询返回与 WT 一致的值
                if (payload.size() < sizeof(protocol::WtStateReportPayload)) {
                    LOG_WARN("DllRecvLoop: WtStateReport payload too small %zu",
                             payload.size());
                    break;
                }
                protocol::WtStateReportPayload wt{};
                std::memcpy(&wt, payload.data(), sizeof(wt));
                auto& vcs = VirtualConsoleState::Instance();
                if (wt.type == 0) {
                    // resize
                    vcs.ApplyWtResize(wt.cols, wt.rows);
                    LOG_INFO("DllRecvLoop: WtStateReport resize cols=%d rows=%d",
                             wt.cols, wt.rows);
                } else if (wt.type == 1) {
                    // cursor report (DSR CPR)
                    vcs.ApplyWtCursorReport(wt.cols, wt.rows);
                    LOG_INFO("DllRecvLoop: WtStateReport cursor col=%d row=%d",
                             wt.cols, wt.rows);
                } else if (wt.type == 2) {
                    // Phase 15：DA report（终端能力标识）
                    vcs.ApplyWtDaReport(wt.cols);
                    LOG_INFO("DllRecvLoop: WtStateReport DA caps=%d",
                             wt.cols);
                }
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
