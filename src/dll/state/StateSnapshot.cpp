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
    return true;
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
    return p;
}

} // namespace terminjector
