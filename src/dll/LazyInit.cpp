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
#include "DllRecvLoop.h"
#include "logging/Logger.h"
#include "transport/ITransport.h"
#include "transport/NamedPipeTransport.h"
#include "protocol/Message.h"
#include "protocol/MessageSerializer.h"
#include "state/StateSnapshot.h"
#include "state/ConsoleState.h"
#include "state/HandleRegistry.h"
#include "state/StatePoller.h"

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
        LOG_INFO("ConsoleState corrected by WT size: %ux%u", wtCols, wtRows);
    }

    // 用 mediator 回传的 ConPTY 光标覆盖 ConsoleState 缓存光标
    // 原因：注入瞬间 snap 拿到的是目标进程私有 ConHost 的光标（如 cmd 已显示 prompt
    //       后的位置），与 WT/ConPTY 的光标（PowerShell 等前置输出之后的位置）不在
    //       同一坐标系。若 DLL 用 ConHost 光标发 VT 定位序列，会把 ConPTY 光标拉到
    //       错误位置，导致注入后头几行输出偏右。改用 ConPTY 光标后，cmd 后续输出
    //       接在 WT 当前位置之后，坐标系与 ConPTY 对齐。
    //       （详见 HelloAckPayload.cursorX/Y 注释）
    {
        COORD conptyCursor;
        conptyCursor.X = static_cast<SHORT>(wtCursorX);
        conptyCursor.Y = static_cast<SHORT>(wtCursorY);
        ConsoleState::Instance().SetCursorPosition(conptyCursor);
        LOG_INFO("ConsoleState cursor aligned to ConPTY: (%u,%u)",
                 wtCursorX, wtCursorY);
    }

    // 5. Phase 5：启动 DLL 侧后台接收线程（处理 ResizeNotify 等控制流）
    //    必须在 g_initialized=true 之前启动，避免 Hook 路径在状态就绪前触发
    StartDllRecvLoop();

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
