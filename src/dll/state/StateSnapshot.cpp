// StateSnapshot 实现：读取真实 Console 状态
// 详见 docs/phases/03-dll-framework.md 4.3.2
//
// 注意：本文件在 Hook 安装前调用，直接用真实 Console API
//       Hook 安装后绝不调用本文件的 Capture（会拿到被 Hook 的假值）
#include "StateSnapshot.h"
#include "logging/Logger.h"

namespace terminjector {

bool StateSnapshot::Capture() {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);
    HANDLE hIn  = GetStdHandle(STD_INPUT_HANDLE);

    // 屏幕缓冲区信息（含光标、窗口、尺寸、属性）
    if (!GetConsoleScreenBufferInfo(hOut, &screenBufferInfo)) {
        LOG_ERROR("Snapshot: GetConsoleScreenBufferInfo failed: %lu", GetLastError());
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
    // 窗口可见性
    windowVisible = IsWindowVisible(GetConsoleWindow());

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

    return true;
}

// Phase 10：读取可见区屏幕内容到 screenCells
// 将 srWindow 区域的内容读入 screenCells，screenRegion 映射到 WT 的 (0,0)
// 后续 LazyInit 用 ConsoleToVt::WriteConsoleOutput 转成 VT 补发给 mediator
void StateSnapshot::CaptureScreenContent() {
    HANDLE hOut = GetStdHandle(STD_OUTPUT_HANDLE);

    // 可见区尺寸
    SMALL_RECT& win = screenBufferInfo.srWindow;
    int width  = win.Right - win.Left + 1;
    int height = win.Bottom - win.Top + 1;
    if (width <= 0 || height <= 0) {
        LOG_WARN("Snapshot: invalid srWindow %dx%d, skip screen capture", width, height);
        return;
    }

    // 分配 CHAR_INFO 矩阵并读取
    screenCells.resize(static_cast<size_t>(width) * height);
    COORD bufSize;
    bufSize.X = static_cast<SHORT>(width);
    bufSize.Y = static_cast<SHORT>(height);
    COORD bufCoord{0, 0};  // 写入 screenCells 的起始位置

    // ReadConsoleOutputW 的 readRegion 是 ConHost 缓冲区坐标（输入+输出）
    // 输入 srWindow，输出实际读取的区域（通常等于 srWindow）
    SMALL_RECT readRegion = win;
    if (!ReadConsoleOutputW(hOut, screenCells.data(), bufSize, bufCoord, &readRegion)) {
        LOG_WARN("Snapshot: ReadConsoleOutputW failed: %lu", GetLastError());
        screenCells.clear();
        return;
    }

    // screenRegion 用于 VT 输出：映射到 WT 的 (0,0)
    // WT 坐标系从 (0,0) 开始，srWindow 内容放在 WT 的 (0,0)-(width-1,height-1)
    screenRegion.Left   = 0;
    screenRegion.Top    = 0;
    screenRegion.Right  = static_cast<SHORT>(width - 1);
    screenRegion.Bottom = static_cast<SHORT>(height - 1);

    LOG_INFO("Snapshot: screen content captured %dx%d (from srWindow %d,%d-%d,%d)",
             width, height, win.Left, win.Top, win.Right, win.Bottom);
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
