// LazyInit 实现：懒加载初始化 + mediator 连接
// 详见 docs/phases/03-dll-framework.md 4.1
//
// 流程（首个 Hook 触发）：
//   1. Logger::Initialize（第一时间启用日志）
//   2. StateSnapshot::Capture（Hook 已启用但本线程在 Detour 内，
//      Capture 直接调真实 API——由于 Hook 拦截的是 WriteConsole*，
//      GetConsoleScreenBufferInfo 等读取 API 不受影响，拿到真实值）
//   3. ConnectToMediatorWithSnapshot（连管道 + 发 Hello + 等 HelloAck）
//   4. ConsoleState::InitFromSnapshot（初始化运行期缓存）
//
// 注意：Capture 调 GetConsoleScreenBufferInfo 等，这些 API 在 Phase 3 未被 Hook，
//       返回真实 ConHost 状态。Phase 5 会 Hook Get* 类 API，届时 Capture 必须在
//       Hook 安装前完成（DllMain 阶段）——但 Phase 3 的 Hook 只有 WriteConsole*，
//       不影响 Get*，故懒加载内 Capture 安全。
#include "LazyInit.h"
#include "BatchSender.h"
#include "DllRecvLoop.h"
#include "logging/Logger.h"
#include "transport/ITransport.h"
#include "transport/NamedPipeTransport.h"
#include "protocol/Message.h"
#include "protocol/MessageSerializer.h"
#include "state/StateSnapshot.h"
#include "state/ConsoleState.h"
#include "state/VirtualConsoleState.h"
#include "state/HandleRegistry.h"
#include "state/StatePoller.h"
#include "translator/ConsoleToVt.h"
#include "translator/VtEscape.h"
#include "hooks/HookCommon.h"

#include <windows.h>
#include <memory>
#include <vector>

namespace terminjector {

namespace {

// 懒加载状态（进程级）
LONG g_initInProgress = 0;   // 0=未开始 1=进行中
bool g_initialized = false;  // 是否完成（含失败）

// 本进程是否为注入目标进程（由 HelloAck.isTarget 设置）
// true=注入目标（DllMain worker 据此 KickStart）；false=子进程
bool g_isTargetProcess = false;

// === 调试探针（Phase 3 端到端验证用，确认懒加载是否触发）===
// extern "C" 保证符号名不修饰，cdb 可直接 dd injected!g_probe_eli 读取
extern "C" volatile LONG g_probe_eli = 0;

// 线程局部标志：当前线程是否正在懒加载中
// 避免懒加载内 Logger 写日志触发 WriteFile Hook → ENSURE_INITIALIZED 死锁
thread_local bool t_inLazyInit = false;

// mediator 传输通道（EnsureLazyInitialized 建立，DLL_PROCESS_DETACH 释放）
std::unique_ptr<ITransport> g_transport;

// 连接 mediator 并完成 Hello 握手（携带快照）
// 返回 true 表示 g_transport 已就绪
// wtCols/wtRows 输出 mediator 回传的 WT 尺寸（用于校正 ConsoleState）
// wtCursorX/wtCursorY 输出 mediator 回传的 WT/ConPTY 当前光标（用于对齐缓存）
// isTarget 输出本进程是否为注入目标进程（HelloAck.isTarget）
bool ConnectToMediatorWithSnapshot(const StateSnapshot& snap,
                                   uint16_t& wtCols, uint16_t& wtRows,
                                   uint16_t& wtCursorX, uint16_t& wtCursorY,
                                   bool& isTarget) {
    using namespace terminjector::protocol;
    wtCols = 0;
    wtRows = 0;
    wtCursorX = 0;
    wtCursorY = 0;
    isTarget = false;

    // 1. 构造管道名（约定：\\.\pipe\terminjector_<targetPid>）
    const uint32_t pid = GetCurrentProcessId();
    const std::wstring pipeName = MakePipeName(pid);
    LOG_INFO("DLL connecting to mediator, pipe=%ls pid=%u", pipeName.c_str(), pid);

    // 2. 创建 Client 传输并连接（内置 5s 重试）
    auto transport = std::make_unique<NamedPipeTransport>(
        pipeName, NamedPipeTransport::Role::Client);
    if (!transport->Connect()) {
        LOG_ERROR("ConnectToMediator: transport Connect failed, pipe=%ls",
                  pipeName.c_str());
        return false;
    }

    // 3. 发送 Hello（携带快照 payload）
    HelloPayload hello = snap.ToHelloPayload();
    auto pkt = Serialize(MessageType::Hello, &hello, sizeof(hello));
    const int sent = transport->Send(pkt.data(), pkt.size());
    if (sent != static_cast<int>(pkt.size())) {
        LOG_ERROR("ConnectToMediator: Send(Hello) failed, sent=%d expected=%zu",
                  sent, pkt.size());
        return false;
    }
    LOG_INFO("Hello sent, pid=%u bitness=%u cols=%u rows=%u cursor=(%u,%u)",
             hello.targetPid, hello.targetBitness,
             hello.bufferCols, hello.bufferRows, hello.cursorX, hello.cursorY);

    // 4. 等待 HelloAck
    MessageType type;
    std::vector<uint8_t> payload;
    if (!RecvPacket(transport.get(), type, payload)) {
        LOG_ERROR("ConnectToMediator: RecvPacket(HelloAck) failed");
        return false;
    }
    if (type != MessageType::HelloAck) {
        LOG_ERROR("ConnectToMediator: expected HelloAck, got type=0x%08X",
                  static_cast<uint32_t>(type));
        return false;
    }

    // 解析 HelloAck payload（mediator 回传 WT 尺寸 + ConPTY 光标 + isTarget 标志）
    if (payload.size() >= sizeof(HelloAckPayload)) {
        HelloAckPayload ack{};
        std::memcpy(&ack, payload.data(), sizeof(ack));
        wtCols = ack.wtCols;
        wtRows = ack.wtRows;
        wtCursorX = ack.cursorX;
        wtCursorY = ack.cursorY;
        isTarget = (ack.isTarget != 0);
        LOG_INFO("HelloAck received, wtCols=%u wtRows=%u cursor=(%u,%u) isTarget=%d, handshake complete",
                 ack.wtCols, ack.wtRows, ack.cursorX, ack.cursorY, isTarget ? 1 : 0);
    } else {
        LOG_INFO("HelloAck received (empty payload), handshake complete");
    }

    g_transport = std::move(transport);
    return true;
}

} // namespace

void EnsureLazyInitialized() {
    // 快速路径：已完成直接返回
    if (g_initialized) return;

    // 当前线程已在懒加载中：直接返回（避免 Logger 写日志触发 WriteFile Hook 死锁）
    if (t_inLazyInit) return;

    // 原子抢占：仅一个线程进入初始化
    if (InterlockedCompareExchange(&g_initInProgress, 1, 0) != 0) {
        // 另一线程正在初始化，自旋等待（Hook 路径不会高频进入此处）
        OutputDebugStringW(L"[terminjector] EnsureLazyInitialized: thread waiting for init");
        while (!g_initialized) Sleep(1);
        OutputDebugStringW(L"[terminjector] EnsureLazyInitialized: thread resumed, init done");
        return;
    }

    // === 仅一个线程执行以下初始化 ===
    t_inLazyInit = true;  // 标记当前线程在懒加载中（Hook 内 pass-through）

    // 1. Logger 第一时间启用（后续步骤的日志可落盘）
    //    每进程独立日志文件：injected_<pid>.log
    //    原因：所有被注入进程（cmd/python/子进程）共用同一日志文件时，
    //          先打开的进程持有写句柄（FILE_SHARE_READ），后续进程无法写入，
    //          导致子进程（如 python）的 DLL 日志丢失，无法诊断
    const uint32_t pid = GetCurrentProcessId();
    wchar_t logPath[260];
    swprintf_s(logPath, L"C:\\temp\\injected_%lu.log", pid);
    Logger::Initialize(logPath, LogLevel::Debug);
    LOG_INFO("=== LazyInit starting, pid=%lu ===", pid);

    // Phase 9：日志文件句柄注册为 protected
    // 防止 Phase 9 CloseHandle Hook 误将日志句柄当作 fake 静默忽略
    // 实际上日志句柄是 FILE_TYPE_DISK，魔数快判断不命中，HandleRegistry::IsFake
    // 也未注册为 fake，CloseHandle 走真实 API。但语义清晰起见仍注册为 protected
    // （未来若 WriteFile Hook 等需要排除 protected 句柄可直接查询）
    void* logHandle = Logger::GetFileHandle();
    if (logHandle != nullptr && logHandle != INVALID_HANDLE_VALUE) {
        HandleRegistry::Instance().RegisterProtected(logHandle);
    }

    // 2. 读取注入瞬间状态快照
    //    Phase 3 仅 Hook WriteConsole*/WriteFile，Get* 类 API 未被拦截，拿到真实值
    StateSnapshot snap;
    if (!snap.Capture()) {
        LOG_WARN("LazyInit: StateSnapshot::Capture failed (no console?), "
                 "continuing with zeroed snapshot");
    }

    // 3. 连接 mediator 并完成 Hello 握手
    //    wtCols/wtRows 为 mediator 回传的 WT 真实尺寸
    //    wtCursorX/Y 为 mediator 回传的 WT/ConPTY 当前光标（对齐缓存用）
    //    isTarget 标识本进程是否为注入目标（控制 KickStart）
    uint16_t wtCols = 0, wtRows = 0;
    uint16_t wtCursorX = 0, wtCursorY = 0;
    bool isTarget = false;
    if (!ConnectToMediatorWithSnapshot(snap, wtCols, wtRows,
                                       wtCursorX, wtCursorY, isTarget)) {
        LOG_FATAL("LazyInit: ConnectToMediator failed, hooks will pass-through");
        // 失败时仍标记已初始化，Hook 走 pass-through（调原 API）
    }
    g_isTargetProcess = isTarget;

    // 4. 初始化运行期状态缓存
    ConsoleState::Instance().InitFromSnapshot(snap);

    // Phase 14：初始化虚拟 Console 状态（从 ConHost 加载初始状态）
    VirtualConsoleState::Instance().InitializeFromConHost();

    // Phase 5：用 mediator 回传的 WT 尺寸校正 ConsoleState
    // 原因：注入瞬间 cmd.exe 的控制台可能尚未初始化完成，
    //       StateSnapshot::Capture 拿到的 dwSize/srWindow 可能是 0x0/1x1，
    //       导致 GetConsoleScreenBufferInfo Hook 返回错误值，cmd 渲染卡死
    //       mediator 的 WT 尺寸是真理来源，应优先采用
    if (wtCols > 0 && wtRows > 0) {
        COORD bufSize;
        bufSize.X = static_cast<SHORT>(wtCols);
        bufSize.Y = static_cast<SHORT>(wtRows);
        ConsoleState::Instance().SetBufferSize(bufSize);
        SMALL_RECT win;
        win.Left   = 0;
        win.Top    = 0;
        win.Right  = static_cast<SHORT>(wtCols - 1);
        win.Bottom = static_cast<SHORT>(wtRows - 1);
        ConsoleState::Instance().SetWindow(win);

        // Phase 14：同步校正 VirtualConsoleState
        VirtualConsoleState::Instance().SetBufferSize(bufSize);
        VirtualConsoleState::Instance().SetWindowRect(win);

        LOG_INFO("ConsoleState corrected by WT size: %ux%u", wtCols, wtRows);
    }

    // Phase 10：补发注入前 ConHost 已有屏幕内容到 WT
    // 原因：注入前 cmd 已输出版本横幅+prompt，这些内容只存在于 ConHost，未发到 WT。
    //       若不补发，WT 空屏光标在 (0,0)，而 ConsoleState 光标在 ConHost 位置（如 (41,3)），
    //       两者坐标系错位，导致后续输出位置错误。
    // 方案：用 ConsoleToVt::WriteConsoleOutput 把 snap.screenCells 转 VT 发给 mediator，
    //       WT 渲染后内容和 ConHost 一致，光标坐标系自然对齐。
    //
    // 注意：WriteConsoleOutput 内部会 SetCursorPosition(0,0)，补发后需手动同步光标到
    //       snap 的正确位置（相对 srWindow 左上角的偏移）
    if (!snap.screenCells.empty()) {
        COORD bufSize;
        bufSize.X = static_cast<SHORT>(snap.screenRegion.Right - snap.screenRegion.Left + 1);
        bufSize.Y = static_cast<SHORT>(snap.screenRegion.Bottom - snap.screenRegion.Top + 1);
        COORD bufCoord{0, 0};
        std::string vt = ConsoleToVt::WriteConsoleOutput(
            snap.screenCells.data(), bufSize, bufCoord, snap.screenRegion);
        hooks::SendToMediator(vt.data(), vt.size());
        LOG_INFO("LazyInit: screen content replayed to WT, %zu bytes", vt.size());

        // 同步 WT 光标到 prompt 末尾位置（相对 srWindow 左上角）
        // ConHost 光标是缓冲区坐标，需减去 srWindow.Left/Top 转为 WT 终端坐标
        COORD cursor = snap.screenBufferInfo.dwCursorPosition;
        SHORT termCursorX = static_cast<SHORT>(cursor.X - snap.screenBufferInfo.srWindow.Left);
        SHORT termCursorY = static_cast<SHORT>(cursor.Y - snap.screenBufferInfo.srWindow.Top);
        // VT CursorPosition 是 1-based
        std::string cursorSync = vt::CursorPosition(termCursorY + 1, termCursorX + 1);
        hooks::SendToMediator(cursorSync.data(), cursorSync.size());
        LOG_INFO("LazyInit: WT cursor synced to terminal (%d,%d)", termCursorX, termCursorY);

        // 关键：ConsoleState 光标设为行首 (0, cursor.Y) 而非 prompt 末尾
        //
        // 原因：cmd 被 KickStart 唤醒后会从 GetConsoleScreenBufferInfo 拿到的光标位置
        //       开始输出新 prompt。若 ConsoleState 光标保持在 prompt 末尾 (cursor.X, cursor.Y)，
        //       cmd 输出的新 prompt 会接在旧 prompt 之后，造成视觉上 prompt 重复：
        //         C:\Users\rikka>C:\Users\rikka>
        //       把 ConsoleState 光标设为行首 (0, cursor.Y) 后：
        //   - WriteConsoleW_Detour 输出前会发 CursorPosition(0, cursor.Y) 同步 WT 光标
        //   - cmd 写新 prompt 字符时从行首开始，正好覆盖补发的旧 prompt
        //   - 新旧 prompt 内容相同（同一工作目录），完全覆盖，视觉无缝
        //
        // 注意：WT 显示的光标此时在 prompt 末尾，但 cmd 第一次 WriteConsoleW 会立即
        //       把 WT 光标拉回行首，用户感知不到错位
        COORD lineStart{0, cursor.Y};
        ConsoleState::Instance().SetCursorPosition(lineStart);
        VirtualConsoleState::Instance().SetCursorPos(lineStart);
        LOG_INFO("LazyInit: ConsoleState cursor set to line start (0,%d) for prompt overwrite",
                 cursor.Y);
    }

    // 5. Phase 5：启动 DLL 侧后台接收线程（处理 ResizeNotify 等控制流）
    //    必须在 g_initialized=true 之前启动，避免 Hook 路径在状态就绪前触发
    StartDllRecvLoop();

    // Phase 10 任务5：启动 BatchSender flush 线程
    // 必须在 g_initialized=true 之前启动：g_initialized 后 Hook 走正常路径，
    // SendToMediator 会把 VtOutput 路由到 BatchSender，此时 BatchSender 必须已就绪
    // 否则 EnqueueVtOutput 走 fallback 直接 Send（功能正确但失去合并优化）
    BatchSender::Instance().Init();

    // Phase 9：隐藏原 Console 窗口
    // 原因：静默模式后原 cmd 黑框不再更新（ConHost 不收到输出），
    //       停在注入前状态会让人误以为卡死，隐藏避免干扰
    // 风险评估：GetConsoleWindow 不 Hook 仍返回真实 HWND（只是隐藏），
    //           程序用 IsWindowVisible 检查可见性会返回 false
    //           极少见程序依赖 Console 窗口可见性，接受此风险
    // 若调试时需查看原 cmd 窗口，注释掉此行
    {
        HWND hCon = GetConsoleWindow();
        if (hCon != nullptr && IsWindowVisible(hCon)) {
            ShowWindow(hCon, SW_HIDE);
            LOG_INFO("LazyInit: original console window hidden (hwnd=%p)", hCon);
        }
    }

    g_initialized = true;
    InterlockedExchange(&g_initInProgress, 0);
    t_inLazyInit = false;
    LOG_INFO("LazyInit done");

    // Phase 10 任务1：启动后台状态轮询线程
    // LazyInit 完成后立即启动，3 秒内 100ms 高频轮询 ConHost 真实状态
    // 捕获 LazyInit 期间其他线程并发输出导致的 ConHost 状态变化（Capture 遗漏项）
    // 3 秒后自动停止（Hook 已完全接管，ConHost 不再变化）
    StatePoller::Instance().Start();
}

bool IsInLazyInit() {
    return t_inLazyInit;
}

ITransport* GetMediatorTransport() {
    return g_transport.get();
}

bool IsLazyInitialized() {
    return g_initialized;
}

// 本进程是否为注入目标进程（HelloAck.isTarget）
// DllMain worker 据此决定是否 KickStart：仅注入目标需要唤醒旧 ReadConsoleW
bool IsTargetProcess() {
    return g_isTargetProcess;
}

// 供 DLL_PROCESS_DETACH 释放传输通道
void ReleaseMediatorTransport() {
    g_transport.reset();
}

} // namespace terminjector
