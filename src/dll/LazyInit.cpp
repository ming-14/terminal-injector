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
#include "HookManager.h"
#include "RemoteParams.h"
#include "logging/Logger.h"
#include "logging/SafeOutputDebugString.h"
#include "transport/ITransport.h"
#include "transport/NamedPipeTransport.h"
#include "protocol/Message.h"
#include "protocol/MessageSerializer.h"
#include "state/StateSnapshot.h"
#include "state/ConsoleState.h"
#include "state/VirtualConsoleState.h"
#include "state/HandleRegistry.h"
#include "state/StatePoller.h"
#include "state/InputQueue.h"
#include "translator/ConsoleToVt.h"
#include "translator/VtEscape.h"
#include "hooks/HookCommon.h"
#include "hooks/BufferHooks.h"
#include "hooks/ProtectionHooks.h"

#include <windows.h>
#include <cstring>
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

// 线程局部标志：当前线程是否正在懒加载中
// 避免懒加载内 Logger 写日志触发 WriteFile Hook → ENSURE_INITIALIZED 死锁
thread_local bool t_inLazyInit = false;

// 注入日志目录：优先 TI_INJECTED_LOG_DIR（测试/诊断覆盖），否则 GetTempPathW()
// （系统标准临时目录，不硬编码任何固定路径，部署到任意机器可用）
std::wstring GetInjectedLogDir() {
    wchar_t envBuf[MAX_PATH] = {0};
    const DWORD n = GetEnvironmentVariableW(L"TI_INJECTED_LOG_DIR", envBuf, MAX_PATH);
    if (n > 0 && n < MAX_PATH) {
        return std::wstring(envBuf);
    }
    wchar_t tmpBuf[MAX_PATH] = {0};
    if (GetTempPathW(MAX_PATH, tmpBuf) == 0) {
        // 极端环境（无 %TEMP%）回退当前目录，避免日志完全丢失
        return std::wstring(L".");
    }
    return std::wstring(tmpBuf);
}

// 构造注入日志文件名：injected_<pid>_<YYYYMMDD-HHMMSS-mmm>.log
// 毫秒时间戳：同一 pid 多次注入（重复会话）与子进程并发各自独立文件，
// 互不覆盖，测试按 mtime 取最新即本次会话
void BuildInjectedLogPath(wchar_t* buf, size_t cap, uint32_t pid) {
    FILETIME ft{};
    GetSystemTimePreciseAsFileTime(&ft);
    SYSTEMTIME st{};
    FileTimeToSystemTime(&ft, &st);
    swprintf_s(buf, cap,
               L"%ls\\injected_%lu_%04u%02u%02u-%02u%02u%02u-%03u.log",
               GetInjectedLogDir().c_str(), pid,
               st.wYear, st.wMonth, st.wDay,
               st.wHour, st.wMinute, st.wSecond, st.wMilliseconds);
}

// 日志级别：TI_LOG_LEVEL 环境变量（TRACE/DEBUG/INFO/WARN/ERROR/FATAL）
// 未设置或非法值默认 Debug（保持既有诊断粒度），保证部署可调且不降级
LogLevel GetConfiguredLogLevel() {
    char envBuf[16] = {0};
    if (GetEnvironmentVariableA("TI_LOG_LEVEL", envBuf, sizeof(envBuf)) > 0) {
        if (_stricmp(envBuf, "TRACE") == 0) return LogLevel::Trace;
        if (_stricmp(envBuf, "DEBUG") == 0) return LogLevel::Debug;
        if (_stricmp(envBuf, "INFO") == 0)  return LogLevel::Info;
        if (_stricmp(envBuf, "WARN") == 0)  return LogLevel::Warn;
        if (_stricmp(envBuf, "ERROR") == 0) return LogLevel::Error;
        if (_stricmp(envBuf, "FATAL") == 0) return LogLevel::Fatal;
    }
    return LogLevel::Debug;
}

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

    // 0. 等待注入器 RemotePipeSetup 传参（随机管道名 + mediatorPid）
    //    注入器在 LoadLibraryW 完成后立即跨进程调用 RemotePipeSetup，
    //    但注入器是独立进程：冷启动 + LoadLibraryW 完成实测需 ~2.4s，
    //    此处轮询最多 5s 兜底（实测注入器传参耗时波动 1~3s）
    PipeParams params{};
    for (int i = 0; i < 100 && !GetPipeParams(params); ++i) {
        Sleep(50);
    }
    if (!GetPipeParams(params)) {
        LOG_ERROR("ConnectToMediator: pipe params not received (RemotePipeSetup "
                  "not called by injector), aborting mediator connect");
        return false;
    }
    LOG_INFO("ConnectToMediator: using injected pipe params, mediatorPid=%u",
             params.mediatorPid);

    // 1. 创建 Client 传输并连接（内置 5s 重试）
    auto transport = std::make_unique<NamedPipeTransport>(
        params.pipeName, NamedPipeTransport::Role::Client);
    if (!transport->Connect()) {
        LOG_ERROR("ConnectToMediator: transport Connect failed, pipe=%ls",
                  params.pipeName);
        return false;
    }

    // 1.5 服务端身份校验（安全加固，防伪造 mediator 抢占）
    //    管道名不可预测是主要防线；此处纵深防御：连接后核对服务端进程
    //    确为注入参数声明的 mediatorPid，不一致立即断开（宁可握手失败）
    if (params.mediatorPid != 0) {
        const uint32_t serverPid = transport->GetServerProcessId();
        if (serverPid == 0 || serverPid != params.mediatorPid) {
            LOG_ERROR("ConnectToMediator: server identity check FAILED, "
                      "serverPid=%u expected=%u, disconnecting",
                      serverPid, params.mediatorPid);
            transport->Disconnect();
            return false;
        }
        LOG_INFO("ConnectToMediator: server identity verified (pid=%u)",
                 serverPid);
    } else {
        LOG_WARN("ConnectToMediator: mediatorPid=0, server identity check skipped "
                 "(manual inject mode)");
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
    LOG_INFO("Hello sent, pid=%u bitness=%u cols=%u rows=%u cursor=(%u,%u) "
             "protocolVersion=%u",
             hello.targetPid, hello.targetBitness,
             hello.bufferCols, hello.bufferRows, hello.cursorX, hello.cursorY,
             kVersion);

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

    // DllMain InstallAll 期间（MinHook 内部调用被 Hook 的 API 如
    // WaitForSingleObjectEx 会命中 Detour）：跳过同步初始化。
    // ConnectToMediator 轮询 RemotePipeSetup 最长 5s，若在 LoadLibraryW
    // 线程（DllMain 上下文）内同步执行，会把注入器侧等待卡 6s+。
    // 由 DllMain 创建的 worker 线程（Sleep 100ms）在 InstallAll 完成后异步初始化。
    if (HookManager::IsInstalling()) return;

    // 当前线程已在懒加载中：直接返回（避免 Logger 写日志触发 WriteFile Hook 死锁）
    if (t_inLazyInit) return;

    // 原子抢占：仅一个线程进入初始化
    if (InterlockedCompareExchange(&g_initInProgress, 1, 0) != 0) {
        // 另一线程正在初始化，自旋等待（Hook 路径不会高频进入此处）
        // 注意：此处必须走 SafeOutputDebugStringW——ODS 内部 DBWIN 等待
        // (WaitForSingleObjectEx) 会被 WaitHooks 拦截 → EnsureLazyInitialized
        // → 再次进入本分支 → 直接 ODS 会无限递归栈溢出（0xC00000FD 崩溃根因，
        // 见 SafeOutputDebugString.h 头部注释）；重入保护在首个 ODS 出口截断
        SafeOutputDebugStringW(L"[terminjector] EnsureLazyInitialized: thread waiting for init");
        while (!g_initialized) Sleep(1);
        SafeOutputDebugStringW(L"[terminjector] EnsureLazyInitialized: thread resumed, init done");
        return;
    }

    // === 仅一个线程执行以下初始化 ===
    t_inLazyInit = true;  // 标记当前线程在懒加载中（Hook 内 pass-through）

    // 1. Logger 第一时间启用（后续步骤的日志可落盘）
    //    每进程独立日志文件：injected_<pid>_<时间戳>.log（GetTempPathW 目录）
    //    原因：所有被注入进程（cmd/python/子进程）共用同一日志文件时，
    //          先打开的进程持有写句柄（FILE_SHARE_READ），后续进程无法写入，
    //          导致子进程（如 python）的 DLL 日志丢失，无法诊断
    //    时间戳精确到毫秒：同一 pid 多次注入互不覆盖
    const uint32_t pid = GetCurrentProcessId();
    wchar_t logPath[MAX_PATH] = {0};
    BuildInjectedLogPath(logPath, MAX_PATH, pid);
    Logger::Initialize(logPath, GetConfiguredLogLevel());
    LOG_INFO("=== LazyInit starting, pid=%lu log=%ls ===", pid, logPath);

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

    // Phase 15：发送 DSR CPR 查询校准 WT 真实光标位置
    // 通过 SendToMediator 发 \x1b[6n 到 mediator，mediator 转发给 WT，
    // WT 响应 \x1b[row;colR 后由 mediator VtParser 检测并发送 WtStateReport(type=1)
    // 给 DLL 更新 VirtualConsoleState，使程序查询到的光标与 WT 一致
    hooks::SendToMediator(vt::kDsrCprQuery, strlen(vt::kDsrCprQuery),
                          protocol::MessageType::VtOutput,
                          /*recordReplay=*/false);
    LOG_INFO("QueryWtCursorPos: DSR CPR query sent to WT");

    // Phase 15：发送 Primary DA 查询获取终端能力标识
    // 通过 SendToMediator 发 \x1b[c 到 mediator，mediator 转发给 WT，
    // WT 响应 \x1b[?1;Psc 后由 mediator VtParser 检测并发送 WtStateReport(type=2)
    // 给 DLL 存储至 VirtualConsoleState，后续可查询终端能力
    hooks::SendToMediator(vt::kDaPrimaryQuery, strlen(vt::kDaPrimaryQuery),
                          protocol::MessageType::VtOutput,
                          /*recordReplay=*/false);
    LOG_INFO("QueryTerminalCaps: Primary DA query sent to WT");

    // Phase 20：按注入瞬间输入模式判定并补发鼠标启用序列
    // 背景：Textual 等 VT 型 TUI 的鼠标启用序列（\x1b[?1000h 等）在注入前
    //       就已写入原 ConHost，被劫持切到 WT 后这些字节已丢失，WT 从未
    //       进入鼠标跟踪模式 → 不发回任何鼠标事件，DLL 的 SGR→KEY_EVENT
    //       转换分支永远命中 0 次。
    // 判定依据：snap.inputMode 含 ENABLE_VIRTUAL_TERMINAL_INPUT(0x200)
    //   - 0x200 未置（如 cmd 的 0x1F7、vim 未开 VT 输入模式）：跳过，
    //     杜绝向普通程序注入鼠标启用序列造成误发。
    //   - 0x200 已置：该进程是 VT 型 TUI，协议上启用鼠标跟踪的写法固定为
    //     DECSET ?1000/?1003/?1015/?1006，补发即可让 WT 进入鼠标模式。
    // 不区分 isTarget：当 cmd 为宿主进程、python(Textual) 为子进程时，
    // TUI 是子进程（isTarget=false），若按 isTarget 门控会漏发；
    // 且重发序列幂等（重复设置同一鼠标模式无副作用），多进程命中无害。
    if (snap.inputMode & ENABLE_VIRTUAL_TERMINAL_INPUT) {
        const char* mouseHi = vt::kEnableMouse;
        const size_t mouseLen = sizeof(vt::kEnableMouse) - 1;  // 去结尾 \0
        hooks::SendToMediator(mouseHi, mouseLen,
                              protocol::MessageType::VtOutput);
        LOG_INFO("Mouse: re-enabled mouse tracking on WT, %zu bytes (inputMode=0x%lx)",
                 mouseLen, snap.inputMode);
    } else {
        LOG_INFO("Mouse: skip mouse re-enable (inputMode=0x%lx)",
                 snap.inputMode);
    }

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
    //
    // Phase 19 修复：仅主目标进程（cmd）执行重放。
    // 子进程（python 等）的 ConHost 快照 = cmd 注入时刻的陈旧内容：
    //   - 重放会覆盖 WT 当前已正确的屏幕（cmd 输出全走 VT 劫持，ConHost 不更新）
    //   - ConHost 快照光标陈旧，用它同步 WT 光标会把光标拉回旧行
    //     （用户反馈：长命令折行后回车，python 输出前 cursorSync 把光标
    //       拉回 ConHost 旧位置（0,4），视觉上"回车后光标跳回折行处"）
    // 子进程光标基准应使用 HelloAck 回传的 WT 真实光标 wtCursorX/Y
    if (!snap.screenCells.empty()) {
        if (isTarget) {
        COORD bufSize;
        bufSize.X = static_cast<SHORT>(snap.screenRegion.Right - snap.screenRegion.Left + 1);
        bufSize.Y = static_cast<SHORT>(snap.screenRegion.Bottom - snap.screenRegion.Top + 1);
        COORD bufCoord{0, 0};
        std::string vt;
        if (kCaptureFullScrollback) {
            // Phase 10 方案 A：全量缓冲（含滚动历史）用流式重放。
            // WriteConsoleOutput 逐行 CursorPosition 绝对定位，行号可达 9000+，
            // 远超 ConPTY 缓冲高度（=视口行数，约 30），越界行被 clamp/覆盖，
            // 内容错位重叠（用户反馈：dir 输出尾部 "32个目录..." 插进 prompt 行）。
            // ReplayScreenStreamed 逐行 \r\n 推进，ConPTY 自然滚动把历史推入
            // WT scrollback，视口恰好停在缓冲底部（= ConHost 可见窗口），
            // 与下方光标换算 cursor.Y - srWindow.Top 的不变量保持一致。
            vt = ConsoleToVt::ReplayScreenStreamed(
                snap.screenCells.data(), bufSize, bufCoord, snap.screenRegion);
        } else {
            vt = ConsoleToVt::WriteConsoleOutput(
                snap.screenCells.data(), bufSize, bufCoord, snap.screenRegion);
        }
        // recordReplay=false：全量重流只发 WT（WT 空屏，需要内容），不进卸载
        // 重放缓冲 VtReplayBuffer。
        // 原因：ConHost 缓冲在注入时冻结，本身已保留这份快照内容（滚动区+可见区）。
        // 若把它也记入重放缓冲，卸载回放到 ConHost 时（视口相对坐标，CUP(1,1)
        // 落在 srWindow.Top 行）会从窗口顶重写全部行，而窗口顶以上的冻结内容
        // 未被覆盖，造成前几行重复（用户反馈：srWindow.Top>0 时顶部历史重复）。
        // 卸载回放只需 [光标同步 + 会话增量]，叠在冻结快照上即可还原会话画面。
        hooks::SendToMediator(vt.data(), vt.size(),
                              protocol::MessageType::VtOutput,
                              /*recordReplay=*/false);
        LOG_INFO("LazyInit: screen content replayed to WT, %zu bytes", vt.size());

        // 同步 WT 光标到 prompt 末尾位置（相对 srWindow 左上角）
        // ConHost 光标是缓冲区坐标，需减去 srWindow.Left/Top 转为 WT 终端坐标
        COORD cursor = snap.screenBufferInfo.dwCursorPosition;
        SHORT termCursorX = static_cast<SHORT>(cursor.X - snap.screenBufferInfo.srWindow.Left);
        SHORT termCursorY = static_cast<SHORT>(cursor.Y - snap.screenBufferInfo.srWindow.Top);
        // VT CursorPosition 是 1-based
        std::string cursorSync = vt::CursorPosition(termCursorY + 1, termCursorX + 1);
        // recordReplay=false：该 CUP 定位到 prompt【末尾】(termCursorX)，只用于
        // WT 光标对齐。若记入卸载重放缓冲，ConHost 重放时先定位到 prompt 末尾，
        // 后续 line-start 的 CUP 又不可靠地移回行首 → 新 prompt 追加到旧 prompt
        // 之后（解除后双 prompt）。卸载重放只需 line-start（行首覆盖）。
        hooks::SendToMediator(cursorSync.data(), cursorSync.size(),
                              protocol::MessageType::VtOutput,
                              /*recordReplay=*/false);
        LOG_INFO("LazyInit: WT cursor synced to terminal (%d,%d)", termCursorX, termCursorY);

        // 注入尺寸对齐（2026-08-08 opencode 实测修复）：
        // 目标 TUI 跑在 42 行控制台而 WT 只有 30 行时（rows=42 vs wtRows=30），
        // 屏幕重放 42 行内容到 30 行 WT 被滚动裁剪、光标同步到第 37 行超界被
        // ConPTY 夹到末行；opencode 持续按 42 行布局重绘 → 光标错位且刷新不归位。
        // 修复：把目标真实 ConHost 窗口 resize 到 WT 尺寸，并向目标进程注入
        // WINDOW_BUFFER_SIZE_EVENT（InputQueue::EnqueueResizeEvent），TUI 程序
        // （opencode/vim/ncurses）收到后按 WT 尺寸重新布局重绘，逐帧正确。
        // 注意：必须放在屏幕重放+光标同步之后——resize 触发的重绘输出
        // （走 WriteFile_Detour 直通 WT）会覆盖重放的临时画面，最终正确。
        // 仅 isTarget：子进程尺寸由 WT 位置决定，不参与对齐。
        if (wtCols > 0 && wtRows > 0 && isTarget) {
            // 取真实控制台句柄：注入目标在 ConPTY 客户端（opencode/vim）里
            // GetStdHandle(STD_OUTPUT_HANDLE) 拿的是 ConPTY 管道句柄，
            // 对其调 GetConsoleScreenBufferInfo / SetConsole* 全部失败——
            // 实测 2026-08-09 verify 里对齐块因此静默跳过、resize 事件未注入。
            // CreateFileW("CONOUT$") 打开的是该进程控制台的 ConHost 屏幕
            // （真实 ConHost 或 ConPTY conhost），尺寸操作与 EnqueueResizeEvent
            // 注入才真正落到目标控制台。
            HANDLE hOut = CreateFileW(
                L"CONOUT$", GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                nullptr, OPEN_EXISTING, 0, nullptr);
            if (hOut != INVALID_HANDLE_VALUE) {
                CONSOLE_SCREEN_BUFFER_INFO cur{};
                // LazyInit 期间 GetConsoleScreenBufferInfo_Detour 走 orig，拿到真实值
                if (GetConsoleScreenBufferInfo(hOut, &cur)) {
                    const SHORT winW = static_cast<SHORT>(
                        cur.srWindow.Right - cur.srWindow.Left + 1);
                    const SHORT winH = static_cast<SHORT>(
                        cur.srWindow.Bottom - cur.srWindow.Top + 1);
                    if (winW != static_cast<SHORT>(wtCols) ||
                        winH != static_cast<SHORT>(wtRows)) {
                        const COORD newBuf{static_cast<SHORT>(wtCols),
                                           static_cast<SHORT>(wtRows)};
                        const SMALL_RECT newWin{0, 0,
                                                static_cast<SHORT>(wtCols - 1),
                                                static_cast<SHORT>(wtRows - 1)};
                        // 走 orig 绕过 BufferHooks Detour（Detour 只更新缓存不调原
                        // API）；真实 ConHost 尺寸与虚拟状态（上方已按 WT 校正）一致。
                        // 顺序必须：先缩窗口、后缩缓冲——缓冲 42 行 > 新缓冲 30 行时
                        // 先缩缓冲会因窗口越界报 ERROR_INVALID_PARAMETER(87)。
                        // 实测（2026-08-08）：SetConsoleScreenBufferSize 在注入进程里
                        // 每次调用被 conhost server 阻塞约 28 秒才返回 err=87
                        // （tall=42 行 alt buffer 场景，疑似 server 端潜在锁/队列），
                        // 8 次重试累计 280 秒——绝对不可接受，仅尝试一次。
                        // 失败走"虚拟对齐"fallback：TUI 收到 resize 事件后按 WT 尺寸
                        // 布局重绘（WriteFile 直通 WT），ConHost 底部 12 行成为未使用
                        // 区，不影响 WT/ConPTY 正确性；ConHost 缓冲尺寸允许保持原值。
                        LOG_INFO("LazyInit: align begin win=%dx%d buf=%dx%d target=%ux%u",
                                 winW, winH,
                                 static_cast<SHORT>(cur.dwSize.X),
                                 static_cast<SHORT>(cur.dwSize.Y),
                                 wtCols, wtRows);
                        const bool winOk = hooks::CallRealSetConsoleWindowInfo(
                            hOut, TRUE, &newWin);
                        const DWORD winErr = GetLastError();
                        LOG_INFO("LazyInit: align SetWindowInfo done winOk=%d err=%lu",
                                 winOk ? 1 : 0, winErr);
                        const bool bufOk = hooks::CallRealSetConsoleScreenBufferSize(
                            hOut, newBuf);
                        const DWORD bufErr = GetLastError();
                        LOG_INFO("LazyInit: align SetScreenBufferSize ok=%d err=%d",
                                 bufOk ? 1 : 0, bufErr);
                        if (winOk && bufOk) {
                            LOG_INFO("LazyInit: real console resize OK "
                                     "(win %dx%d -> %ux%u)",
                                     winW, winH, wtCols, wtRows);
                        } else {
                            LOG_WARN("LazyInit: align target console to %ux%u failed "
                                     "(win=%d err=%lu, buf=%d err=%lu), "
                                     "using virtual alignment fallback",
                                     wtCols, wtRows, winOk ? 1 : 0, winErr,
                                     bufOk ? 1 : 0, bufErr);
                        }
                        // 无论真实 resize 是否成功，都注入 resize 事件让 TUI 按 WT
                        // 尺寸重绘（虚拟对齐成功路径的 B 部分；真对齐失败时是唯一
                        // 保证 ConPTY 30 行布局正确的路径）。ConHost 底部多余行在
                        // TUI 重绘后成为未使用区。
                        InputQueue::Instance().EnqueueResizeEvent(
                            static_cast<SHORT>(wtCols), static_cast<SHORT>(wtRows));
                        LOG_INFO("LazyInit: target console aligned to WT %ux%u "
                                 "(was win %dx%d), resize event injected for TUI relayout",
                                 wtCols, wtRows, winW, winH);
                    } else {
                        LOG_INFO("LazyInit: target console size matches WT %ux%u, "
                                 "skip alignment", wtCols, wtRows);
                    }
                }
                CloseHandle(hOut);
            }
        }

        // BUG-002：重放结束后补发 SGR 重置，把 VT 流恢复到默认状态。
        // 快照 cell 可能含反显/下划线/颜色（如 pwsh 横幅 0x4007），状态由
        // 重放线程输出；重放线程的 thread_local SGR 缓存与主线程隔离，
        // 若不加重置，主线程首条输出（t_lastAttr 未初始化）不会发出关闭码，
        // 剩余的反显/下划线会泄漏到后续输出。
        {
            const char kResetSgr[] = "\x1b[0m";
            hooks::SendToMediator(kResetSgr, sizeof(kResetSgr) - 1);
        }

        // 关键是: 行首覆盖 仅适用于"行编辑 shell"(cmd/pwsh/python REPL)。
        //
        // 背景: cmd 被 KickStart 唤醒后从 GetConsoleScreenBufferInfo 读到的在行首位置
        //       开始输出新 prompt。若光标保持在 prompt 末尾 (cursor.X, cursor.Y)，新 prompt
        //       会接在旧 prompt 之后，造成视觉上 prompt 重复: <旧prompt><新prompt>...
        //       把 ConsoleState 光标设为行首 (0, cursor.Y) 后:
        //   - WriteConsoleW_Detour 输出前会发 CursorPosition(0, cursor.Y) 同步 WT 光标
        //   - cmd 写新 prompt 字符时从行首开始，正好覆盖补发的旧 prompt
        //   - 新旧 prompt 内容相同（同一工作目录），完全覆盖，视觉无缝
        //
        // 识别依据（实证）: 行编辑 shell 的 inputMode 含 ENABLE_ECHO_INPUT(0x4):
        //   cmd  in=0x1f7  pwsh in=0x1e4  → 行编辑 shell，需行首覆盖
        //   vim  in=0x1b8  Textual(vt)    → 不含 0x4，全屏 TUI
        // 全屏 TUI(vim/Textual/ncurses)自身管理光标，不重打 prompt:
        //   - ConPTY/WT 光标必须停在 TUI 的真实光标 (termCursorX, termCursorY)
        //     （上面已同步），否则光标错位(用户反馈：劫持后光标不定位到应用的编辑位置)
        //   - 不做行首覆盖，后续 VT 输出由 TUI 自己用绝对/相对定位控制
        //
        // TUI-CURSOR-BUG 修复（2026-08-08 实测）：
        // 仅凭 ECHO_INPUT 判别会把"未改输入模式的 VT 全屏 TUI"误判为行编辑 shell。
        // 实测 python 全屏 VT 脚本 (inputMode=0x1f7 含 ECHO_INPUT, 已切 alt buffer)：
        //   - ConHost 光标 = (44,23)，DLL 发 CUP(24;45) 后 ConPTY 光标正确 = (44,23)
        //   - 随后行首覆盖 CUP(24;1) 把 ConPTY/WT 光标拉进左 gutter → 用户反馈的错位
        // 补充信号：alt buffer 无滚动历史 ⇒ 缓冲区尺寸 == 窗口尺寸；
        // 行编辑 shell (cmd/pwsh) 的缓冲区远高于窗口（9001 行滚动），不受影响。
        const bool echoInput = (snap.inputMode & ENABLE_ECHO_INPUT) != 0;
        const SHORT winW = static_cast<SHORT>(
            snap.screenBufferInfo.srWindow.Right - snap.screenBufferInfo.srWindow.Left + 1);
        const SHORT winH = static_cast<SHORT>(
            snap.screenBufferInfo.srWindow.Bottom - snap.screenBufferInfo.srWindow.Top + 1);
        const bool bufMatchesWin = snap.screenBufferInfo.dwSize.X == winW &&
                                   snap.screenBufferInfo.dwSize.Y == winH;
        const bool isLineShell = echoInput && !bufMatchesWin;
        if (isLineShell) {
            // 行编辑 shell：ConsoleState 光标设行首，覆盖补发的旧 prompt
            COORD lineStart{0, cursor.Y};
            ConsoleState::Instance().SetCursorPosition(lineStart);
            VirtualConsoleState::Instance().SetCursorPos(lineStart);
            LOG_INFO("LazyInit: ConsoleState cursor set to line start (0,%d) for prompt overwrite",
                     cursor.Y);

            // Phase 20 修复：VT 模式下 prompt 连排
            // WriteConsoleW_Detour / WriteFile_Detour 的 VT 直通分支（OutputHooks.cpp）
            // 不发送前置 CursorPosition（避免污染 TUI 程序的 VT）。
            // ConPTY 光标仍停在上面同步的旧 prompt 末尾 (cursor.X, cursor.Y)，
            // cmd 被 KillStart 唤醒后输出的新 prompt 会被追加其后，形成
            //   <prompt><prompt>
            // 连排。此处主动把 ConPTY 光标拉到行首，使翻译/VT 直通两种模式下
            // 新 prompt 都从行首写入，正好覆盖补发的旧 prompt，视觉无缝。
            {
                std::string lineStartSync = vt::CursorPosition(termCursorY + 1, 1);
                // recordReplay=false：该 CUP 只用于把 WT/ConPTY 光标拉到行首
                // （覆盖旧 prompt）。ConHost 卸载重放时由 Unloader 用
                // SetConsoleCursorPosition 归位 + 纯文本重放，CUP 视口相对行号
                // 在 ConHost 上不可靠（落错位置 → 新 prompt 拼到旧 prompt 之后）。
                hooks::SendToMediator(lineStartSync.data(), lineStartSync.size(),
                                      protocol::MessageType::VtOutput,
                                      /*recordReplay=*/false);
                LOG_INFO("LazyInit: ConPTY cursor pulled to line start (shell prompt overwrite)");
            }
        } else {
            // 全屏 TUI：不执行行首覆盖，光标停留在上面已同步的真实位置
            // (termCursorY+1, termCursorX+1)。ConsoleState/Virtual 状态保持
            // 快照光标 (=应用自身光标)，保证 GetConsoleScreenBufferInfo 返回一致。
            // 注意：TUI 分支不要求"无 ECHO_INPUT"——未改输入模式的 VT 全屏程序
            // (如 python VT 探针 inputMode=0x1f7) 也可能含 ECHO_INPUT，
            // 判别依据是 bufMatchesWin（alt buffer 缓冲==窗口，见上注释）。
            LOG_INFO("LazyInit: fullscreen TUI (inputMode=0x%lx, buf=%dx%d win=%dx%d, "
                     "isLineShell=false): cursor kept at (%d,%d), skip prompt overwrite",
                     snap.inputMode,
                     snap.screenBufferInfo.dwSize.X, snap.screenBufferInfo.dwSize.Y,
                     winW, winH, termCursorX, termCursorY);
        }
        } else {
            // 子进程：不重放屏幕，用 HelloAck 回传的 WT 真实光标对齐缓存
            // （ChildSession Handshake 注释：cursorX/Y 供子进程 DLL 对齐
            //   ConsoleState 光标缓存，使子进程输出接在 WT 当前位置之后）
            if (wtCursorX > 0 || wtCursorY > 0) {
                COORD c;
                c.X = static_cast<SHORT>(wtCursorX);
                c.Y = static_cast<SHORT>(wtCursorY);
                ConsoleState::Instance().SetCursorPosition(c);
                VirtualConsoleState::Instance().SetCursorPos(c);
                LOG_INFO("LazyInit: child cursor aligned to WT (%u,%u) from HelloAck",
                         wtCursorX, wtCursorY);
            }
        }
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
    // 注意：必须走 CallRealGetConsoleWindow（GetConsoleWindow 已 Hook 返回 NULL）
    // 若调试时需查看原 cmd 窗口，注释掉此行
    {
        HWND hCon = hooks::CallRealGetConsoleWindow();
        if (hCon != nullptr && IsWindowVisible(hCon)) {
            ShowWindow(hCon, SW_HIDE);
            LOG_INFO("LazyInit: original console window hidden (hwnd=%p)", hCon);
        }
    }

    g_initialized = true;
    InterlockedExchange(&g_initInProgress, 0);
    t_inLazyInit = false;
    LOG_INFO("LazyInit done (hooksInstalled=%d registered=%zu)",
             HookManager::IsInstalled() ? 1 : 0,
             HookManager::RegisteredCount());

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
