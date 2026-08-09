// StateSnapshot 实现：读取真实 Console 状态
// 详见 docs/phases/03-dll-framework.md 4.3.2
//
// 注意：本文件在 Hook 安装前调用，直接用真实 Console API
//       Hook 安装后绝不调用本文件的 Capture（会拿到被 Hook 的假值）
#include "StateSnapshot.h"
#include "../hooks/ProtectionHooks.h"
#include "logging/Logger.h"

#include <utility>

namespace terminjector {

namespace {

// 获取可用于 console API 的输出句柄。
// 某些场景（cmd 批处理等待子进程等）下 GetStdHandle(STD_OUTPUT_HANDLE) 返回的
// 句柄对 console API 无效（GetConsoleScreenBufferInfo 报 err=6 ERROR_INVALID_HANDLE），
// 而 CONOUT$ 打开的总是当前进程真实控制台。故优先用 std 句柄，失败回退 CONOUT$。
// 返回 (handle, shouldClose)：shouldClose=true 时调用方用完需 CloseHandle（仅回退路径）。
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

bool StateSnapshot::Capture() {
    const auto [hOut, hCloseOut] = GetConsoleOutHandle();
    HANDLE hIn  = GetStdHandle(STD_INPUT_HANDLE);

    // 屏幕缓冲区信息（含光标、窗口、尺寸、属性）
    if (!GetConsoleScreenBufferInfo(hOut, &screenBufferInfo)) {
        LOG_ERROR("Snapshot: GetConsoleScreenBufferInfo failed: %lu", GetLastError());
        if (hCloseOut) CloseHandle(hOut);
        return false;
    }
    // 光标显隐与大小（失败仅警告，不中断）
    if (!GetConsoleCursorInfo(hOut, &cursorInfo)) {
        LOG_WARN("Snapshot: GetConsoleCursorInfo failed: %lu", GetLastError());
    }
    // 字体信息
    fontInfo.cbSize = sizeof(fontInfo);
    if (!GetCurrentConsoleFontEx(hOut, FALSE, &fontInfo)) {
        LOG_WARN("Snapshot: GetCurrentConsoleFontEx failed: %lu", GetLastError());
    }
    // 输入/输出模式
    GetConsoleMode(hIn, &inputMode);
    GetConsoleMode(hOut, &outputMode);
    // 代码页
    inputCp  = GetConsoleCP();
    outputCp = GetConsoleOutputCP();
    // 标题
    if (!GetConsoleTitleW(title, 260)) {
        title[0] = L'\0';
    }
    // 窗口可见性（GetConsoleWindow 已 Hook 返回 NULL，走 orig 拿真实 HWND）
    windowVisible = IsWindowVisible(hooks::CallRealGetConsoleWindow());

    LOG_INFO("Snapshot: size=%dx%d win=%dx%d cursor=(%d,%d) mode(in=0x%lx out=0x%lx) cp(in=%u out=%u)",
             screenBufferInfo.dwSize.X, screenBufferInfo.dwSize.Y,
             screenBufferInfo.srWindow.Right - screenBufferInfo.srWindow.Left + 1,
             screenBufferInfo.srWindow.Bottom - screenBufferInfo.srWindow.Top + 1,
             screenBufferInfo.dwCursorPosition.X, screenBufferInfo.dwCursorPosition.Y,
             inputMode, outputMode, inputCp, outputCp);

    // Phase 10：读取可见区屏幕内容（srWindow 区域的 CHAR_INFO 矩阵）
    // 用途：注入前 cmd 已输出（版本横幅+prompt）只存在于 ConHost，未发到 WT。
    //       握手后把这块内容补发给 WT，让 WT 显示和 ConHost 一致的内容，
    //       光标位置才能对齐（否则 WT 空屏光标在 0,0，ConHost 光标在 4,41）
    // ReadConsoleOutputW 未被 Hook，直接读真实 ConHost 屏幕缓冲区
    CaptureScreenContent();

    // 回退路径打开的 CONOUT$ 句柄用完即关（std 句柄路径 hCloseOut=false）
    if (hCloseOut) CloseHandle(hOut);

    return true;
}

// 读取指定 ConHost 缓冲区域到 screenCells（screenRegion 映射到 WT 的 (0,0)）
// 返回 false 表示读取失败（screenCells 已清空，screenRegion 未设置）
bool StateSnapshot::CaptureRegion(SMALL_RECT region) {
    const auto [hOut, hCloseOut] = GetConsoleOutHandle();

    int width  = region.Right - region.Left + 1;
    int height = region.Bottom - region.Top + 1;
    if (width <= 0 || height <= 0) {
        if (hCloseOut) CloseHandle(hOut);
        LOG_WARN("Snapshot: invalid region %dx%d, skip screen capture", width, height);
        return false;
    }

    // 分配 CHAR_INFO 矩阵并读取
    screenCells.resize(static_cast<size_t>(width) * height);
    COORD bufSize;
    bufSize.X = static_cast<SHORT>(width);
    bufSize.Y = static_cast<SHORT>(height);
    COORD bufCoord{0, 0};  // 写入 screenCells 的起始位置

    // ReadConsoleOutputW 的 readRegion 是 ConHost 缓冲区坐标（输入+输出）
    // 输入 region，输出实际读取的区域（通常等于 region）
    SMALL_RECT readRegion = region;
    if (!ReadConsoleOutputW(hOut, screenCells.data(), bufSize, bufCoord, &readRegion)) {
        LOG_WARN("Snapshot: ReadConsoleOutputW(%dx%d) failed: %lu",
                 width, height, GetLastError());
        screenCells.clear();
        if (hCloseOut) CloseHandle(hOut);
        return false;
    }
    if (hCloseOut) CloseHandle(hOut);

    // screenRegion 用于 VT 输出：映射到 WT 的 (0,0)
    // WT 坐标系从 (0,0) 开始，抓取区域放在 WT 的 (0,0)-(width-1,height-1)
    screenRegion.Left   = 0;
    screenRegion.Top    = 0;
    screenRegion.Right  = static_cast<SHORT>(width - 1);
    screenRegion.Bottom = static_cast<SHORT>(height - 1);
    return true;
}

// 读取屏幕内容到 screenCells
// 区域由 kCaptureFullScrollback 决定：
//   - true：整个屏幕缓冲区（dwSize，含滚动历史），screenRegion=dwSize
//   - false：仅 srWindow 可见区
// 后续 LazyInit 用 ConsoleToVt::WriteConsoleOutput 转成 VT 补发给 mediator
// 注意：读整个缓冲时（9001 行 × 120 列）ReadConsoleOutputW 一次调用即可完成，
//       CHAR_INFO 矩阵 ~4.3MB，无需分块；若 ConHost 缓冲异常巨大导致失败，
//       回退到仅读可见区保证握手不中断。
void StateSnapshot::CaptureScreenContent() {
    if (kCaptureFullScrollback) {
        // 全量：整个屏幕缓冲区（含滚动历史）
        SMALL_RECT full = {0, 0,
                           static_cast<SHORT>(screenBufferInfo.dwSize.X - 1),
                           static_cast<SHORT>(screenBufferInfo.dwSize.Y - 1)};
        if (CaptureRegion(full)) {
            LOG_INFO("Snapshot: screen content captured %dx%d (full buffer incl. scrollback)",
                     full.Right - full.Left + 1, full.Bottom - full.Top + 1);
            return;
        }
        // 全量读取失败（罕见）：回退到仅可见区，保证注入流程继续
        LOG_WARN("Snapshot: full-buffer capture failed, fallback to srWindow-only");
    }
    if (CaptureRegion(screenBufferInfo.srWindow)) {
        LOG_INFO("Snapshot: screen content captured %dx%d (srWindow visible region)",
                 screenBufferInfo.srWindow.Right - screenBufferInfo.srWindow.Left + 1,
                 screenBufferInfo.srWindow.Bottom - screenBufferInfo.srWindow.Top + 1);
    }
}

protocol::HelloPayload StateSnapshot::ToHelloPayload() const {
    protocol::HelloPayload p{};
    p.targetPid = GetCurrentProcessId();
    p.targetBitness = 64;
    p.consoleMode = static_cast<uint16_t>(outputMode);
    p.consoleCp = static_cast<uint16_t>(inputCp);
    p.consoleOutputCp = static_cast<uint16_t>(outputCp);
    p.bufferCols = static_cast<uint16_t>(screenBufferInfo.dwSize.X);
    p.bufferRows = static_cast<uint16_t>(screenBufferInfo.dwSize.Y);
    p.cursorX = static_cast<uint16_t>(screenBufferInfo.dwCursorPosition.X);
    p.cursorY = static_cast<uint16_t>(screenBufferInfo.dwCursorPosition.Y);
    // 可见窗口高度（bufferCols 恒等于窗口宽，故无需单独存 windowCols）
    p.windowRows = static_cast<uint16_t>(
        screenBufferInfo.srWindow.Bottom - screenBufferInfo.srWindow.Top + 1);
    // Phase 11：上报 injected.dll 的 HMODULE 给 mediator
    // mediator 收到 UnloadComplete 后用 CreateRemoteThread 远程调
    // FreeLibrary(dllBase) 触发 DETACH（DLL 内部无法让 LoadCount 归零，
    // 因 cmd 主线程 LdrpThreadBlob 持引用）
    HMODULE hSelf = GetModuleHandleW(L"injected.dll");
    p.dllBase = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(hSelf));
    return p;
}

} // namespace terminjector
