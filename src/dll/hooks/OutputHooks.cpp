// OutputHooks 实现：全部输出类 Console API 的 Hook
// 详见 docs/phases/03-dll-framework.md 4.5.3 与 docs/phases/04-output-chain.md
//
// Phase 3：WriteConsoleW/A + WriteFile（文本流输出）
// Phase 4：补全 SetConsoleTextAttribute / FillConsoleOutput* /
//          WriteConsoleOutput* / ScrollConsoleScreenBuffer（矩阵/属性/滚屏）
// Phase 9：所有输出类 Hook 改为静默模式（不调 _orig），消除原 cmd 黑框闪烁
//
// 流程：
//   1. ENSURE_INITIALIZED() 触发懒加载
//   2. 非真实 Console 句柄（如日志文件）直接调原 API（pass-through）
//   3. 翻译为 VT 序列，SendToMediator 发给 mediator
//   4. 更新光标/属性缓存（ConsoleState）
//   5. 设置 lpNumberOfCharsWritten 等输出参数，返回 TRUE（不调 _orig）
//
// A 版本统一走 A→W 转换后复用 W 路径的翻译逻辑
//
// 重要：cmd.exe 等程序通过 WriteFile 写控制台输出（而非 WriteConsoleA），
//       WriteFile 内部直接走系统调用，不经过 WriteConsoleA 导出函数，
//       故必须 Hook WriteFile 才能拦截 cmd 输出
#include "OutputHooks.h"
#include "HookCommon.h"
#include "HookWhitelist.h"
#include "../HookManager.h"
#include "../state/ConsoleState.h"
#include "../state/VirtualConsoleState.h"
#include "../translator/ConsoleToVt.h"
#include "../translator/VtEscape.h"
#include "logging/Logger.h"

#include <windows.h>
#include <string>
#include <vector>

namespace terminjector::hooks {

// 引入 VT 序列生成函数（SgrFromAttribute 等）
using terminjector::vt::SgrFromAttribute;
using terminjector::vt::CursorPosition;

// ============================================================
// 原函数指针定义
// ============================================================

// Phase 3：文本流输出
DEFINE_ORIG_PTR(WriteConsoleW, BOOL WINAPI(
    HANDLE hConsoleOutput, const VOID* lpBuffer,
    DWORD nNumberOfCharsToWrite, LPDWORD lpNumberOfCharsWritten, LPVOID lpReserved));
DEFINE_ORIG_PTR(WriteConsoleA, BOOL WINAPI(
    HANDLE hConsoleOutput, const VOID* lpBuffer,
    DWORD nNumberOfCharsToWrite, LPDWORD lpNumberOfCharsWritten, LPVOID lpReserved));
DEFINE_ORIG_PTR(WriteFile, BOOL WINAPI(
    HANDLE hFile, LPCVOID lpBuffer,
    DWORD nNumberOfBytesToWrite, LPDWORD lpNumberOfBytesWritten,
    LPOVERLAPPED lpOverlapped));

// Phase 4：属性设置
DEFINE_ORIG_PTR(SetConsoleTextAttribute, BOOL WINAPI(HANDLE, WORD));

// Phase 4：填充输出
DEFINE_ORIG_PTR(FillConsoleOutputCharacterW, BOOL WINAPI(
    HANDLE, wchar_t, DWORD, COORD, LPDWORD));
DEFINE_ORIG_PTR(FillConsoleOutputCharacterA, BOOL WINAPI(
    HANDLE, char, DWORD, COORD, LPDWORD));
DEFINE_ORIG_PTR(FillConsoleOutputAttribute, BOOL WINAPI(
    HANDLE, WORD, DWORD, COORD, LPDWORD));

// Phase 4：矩阵输出
DEFINE_ORIG_PTR(WriteConsoleOutputW, BOOL WINAPI(
    HANDLE, const CHAR_INFO*, COORD, COORD, PSMALL_RECT));
DEFINE_ORIG_PTR(WriteConsoleOutputA, BOOL WINAPI(
    HANDLE, const CHAR_INFO*, COORD, COORD, PSMALL_RECT));
DEFINE_ORIG_PTR(WriteConsoleOutputCharacterW, BOOL WINAPI(
    HANDLE, const wchar_t*, DWORD, COORD, LPDWORD));
DEFINE_ORIG_PTR(WriteConsoleOutputCharacterA, BOOL WINAPI(
    HANDLE, const char*, DWORD, COORD, LPDWORD));

// Phase 4：滚屏
DEFINE_ORIG_PTR(ScrollConsoleScreenBufferW, BOOL WINAPI(
    HANDLE, const SMALL_RECT*, const SMALL_RECT*, COORD, const CHAR_INFO*));
DEFINE_ORIG_PTR(ScrollConsoleScreenBufferA, BOOL WINAPI(
    HANDLE, const SMALL_RECT*, const SMALL_RECT*, COORD, const CHAR_INFO*));

// === 调试探针（Phase 3 端到端验证用，确认各 Detour 是否被 cmd 调用）===
// extern "C" 保证符号名不修饰，cdb 可直接 dd injected!g_probe_wf 读取
extern "C" volatile LONG g_probe_wcw = 0;  // WriteConsoleW_Detour 调用计数
extern "C" volatile LONG g_probe_wca = 0;  // WriteConsoleA_Detour 调用计数
extern "C" volatile LONG g_probe_wf  = 0;  // WriteFile_Detour 调用计数

// ============================================================
// Phase 3：文本流输出 Hook
// ============================================================

// Hook 实现：WriteConsoleW
BOOL WINAPI WriteConsoleW_Detour(
    HANDLE hConsoleOutput, const VOID* lpBuffer,
    DWORD nNumberOfCharsToWrite, LPDWORD lpNumberOfCharsWritten, LPVOID lpReserved) {

    // 懒加载触发（首个 Hook 调用初始化 Logger/Connect/State）
    ENSURE_INITIALIZED();
    ASSERT_IN_HOOK();          // 关键 Detour：输出主路径，A→W 复用终点，Logger 重入风险
    HookReentryGuard guard;

    // 非真实 Console 句柄（如日志文件句柄）直接 pass-through
    if (!IsConsoleHandle(hConsoleOutput)) {
        return WriteConsoleW_orig(hConsoleOutput, lpBuffer,
                                  nNumberOfCharsToWrite,
                                  lpNumberOfCharsWritten, lpReserved);
    }

    // 诊断日志（排查 Python 双 >>> 问题，验证后移除）
    {
        COORD before = ConsoleState::Instance().GetCursorPosition();
        const wchar_t* wbuf = reinterpret_cast<const wchar_t*>(lpBuffer);
        LOG_INFO("WriteConsoleW_Detour: len=%lu beforeCursor=(%d,%d) firstChars=0x%04X,0x%04X,0x%04X,0x%04X",
                 nNumberOfCharsToWrite, before.X, before.Y,
                 nNumberOfCharsToWrite > 0 ? static_cast<unsigned>(wbuf[0]) : 0,
                 nNumberOfCharsToWrite > 1 ? static_cast<unsigned>(wbuf[1]) : 0,
                 nNumberOfCharsToWrite > 2 ? static_cast<unsigned>(wbuf[2]) : 0,
                 nNumberOfCharsToWrite > 3 ? static_cast<unsigned>(wbuf[3]) : 0);
    }

    auto& state = ConsoleState::Instance();

    // 修复 ConPTY 光标不同步：输出前强制同步 ConPTY 光标到 DLL 缓存位置
    //
    // 根因：mediator 把 DLL 的 VT 写入 ConPTY，ConPTY 维护自己的虚拟光标。
    // DLL 发纯文本（如 >>> ）时不带光标定位，ConPTY 用自己的光标写入。
    // Python banner 等多行输出后，ConPTY 光标（随文本推进）与 DLL 的 AdvanceCursor
    // 缓存可能因换行/自动换行处理差异而偏移，导致后续 >>> 写到错误位置。
    // 之后 SetConsoleCursorPosition 的光标回退序列把 ConPTY 光标拉回正确位置，
    // 第二个 >>> 写到正确位置，形成"双 >>> 在不同位置"现象。
    //
    // 修复：每次 WriteConsoleW 前发送 CursorPosition，强制 ConPTY 光标 = DLL 缓存，
    // 确保 ConPTY 写入位置与 DLL 预期一致。开销仅 7 字节/次，可接受。
    {
        COORD cur = state.GetCursorPosition();
        std::string cursorSync = CursorPosition(cur.Y + 1, cur.X + 1);
        SendToMediator(cursorSync.data(), cursorSync.size());
    }

    // 翻译为 VT 序列并发给 mediator
    WORD attr = state.GetTextAttribute();
    std::string vt = ConsoleToVt::WriteConsoleW(
        reinterpret_cast<const wchar_t*>(lpBuffer), nNumberOfCharsToWrite, attr);
    SendToMediator(vt.data(), vt.size());

    // 更新光标缓存（解析 \r \n \b \t 控制字符，行末换行，Phase 5 补全滚屏）
    state.AdvanceCursor(reinterpret_cast<const wchar_t*>(lpBuffer),
                        static_cast<int>(nNumberOfCharsToWrite),
                        /*wrapAtEol=*/true);

    // Phase 14：同步更新 VirtualConsoleState 光标
    VirtualConsoleState::Instance().AdvanceCursor(
        reinterpret_cast<const wchar_t*>(lpBuffer),
        static_cast<int>(nNumberOfCharsToWrite));

    // 诊断日志：AdvanceCursor 后
    {
        COORD after = ConsoleState::Instance().GetCursorPosition();
        LOG_INFO("WriteConsoleW_Detour: afterCursor=(%d,%d)", after.X, after.Y);
    }

    // Phase 9：不调原 API，消除原 cmd 黑框闪烁
    // ConHost 不再收到任何输出，原 cmd 窗口停止更新
    if (lpNumberOfCharsWritten != nullptr) {
        *lpNumberOfCharsWritten = nNumberOfCharsToWrite;
    }
    return TRUE;
}

// Hook 实现：WriteConsoleA（A→W 转换后复用 W 路径）
BOOL WINAPI WriteConsoleA_Detour(
    HANDLE hConsoleOutput, const VOID* lpBuffer,
    DWORD nNumberOfCharsToWrite, LPDWORD lpNumberOfCharsWritten, LPVOID lpReserved) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return WriteConsoleA_orig(hConsoleOutput, lpBuffer,
                                  nNumberOfCharsToWrite,
                                  lpNumberOfCharsWritten, lpReserved);
    }

    // A → W 转换（按当前输出代码页）
    UINT cp = ConsoleState::Instance().GetOutputCp();
    int wlen = MultiByteToWideChar(cp, 0, reinterpret_cast<const char*>(lpBuffer),
                                   static_cast<int>(nNumberOfCharsToWrite), nullptr, 0);
    if (wlen <= 0) {
        // 转换失败，直接调原 API
        return WriteConsoleA_orig(hConsoleOutput, lpBuffer,
                                  nNumberOfCharsToWrite,
                                  lpNumberOfCharsWritten, lpReserved);
    }
    std::wstring wbuf(static_cast<size_t>(wlen), L'\0');
    MultiByteToWideChar(cp, 0, reinterpret_cast<const char*>(lpBuffer),
                        static_cast<int>(nNumberOfCharsToWrite), wbuf.data(), wlen);

    // 复用 W 路径（注意：传入的是 wlen 而非 nNumberOfCharsToWrite）
    return WriteConsoleW_Detour(hConsoleOutput, wbuf.data(), static_cast<DWORD>(wlen),
                                lpNumberOfCharsWritten, lpReserved);
}

// Hook 实现：WriteFile
// cmd.exe 等程序通过 WriteFile 写控制台输出，必须拦截
// 非控制台句柄（文件/管道等）直接 pass-through，性能无影响
BOOL WINAPI WriteFile_Detour(
    HANDLE hFile, LPCVOID lpBuffer,
    DWORD nNumberOfBytesToWrite, LPDWORD lpNumberOfBytesWritten,
    LPOVERLAPPED lpOverlapped) {

    // === 调试探针 ===
    InterlockedIncrement(&g_probe_wf);
    OutputDebugStringW(L"[terminjector-probe] WriteFile_Detour ENTERED");

    // 懒加载中的线程直接 pass-through
    // 原因：Logger::LogImpl 内部调 WriteFile 写日志文件，
    //       若不跳过会触发 ENSURE_INITIALIZED → 懒加载 → Logger → WriteFile 死锁
    if (IsInLazyInit()) {
        return WriteFile_orig(hFile, lpBuffer, nNumberOfBytesToWrite,
                              lpNumberOfBytesWritten, lpOverlapped);
    }

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    // === 诊断：ENSURE_INITIALIZED 后，记录文件句柄类型 ===
    {
        char dbg[128];
        std::snprintf(dbg, sizeof(dbg),
            "[terminjector] WriteFile_Detour: past ENSURE_INIT, hFile=%p fileType=%lu",
            hFile, GetFileType(hFile));
        wchar_t wdbg[256];
        int wl = MultiByteToWideChar(CP_UTF8, 0, dbg, -1, wdbg, 256);
        if (wl > 0) OutputDebugStringW(wdbg);
    }

    // 异步 I/O 直接 pass-through（控制台很少用异步写）
    if (lpOverlapped != nullptr) {
        return WriteFile_orig(hFile, lpBuffer, nNumberOfBytesToWrite,
                              lpNumberOfBytesWritten, lpOverlapped);
    }

    // 非控制台句柄（日志文件、普通文件等）直接 pass-through
    // IsConsoleHandle 用 GetFileType 快速过滤，性能可接受
    if (!IsConsoleHandle(hFile)) {
        return WriteFile_orig(hFile, lpBuffer, nNumberOfBytesToWrite,
                              lpNumberOfBytesWritten, lpOverlapped);
    }

    // Phase 13：VT 输出直通模式
    // 当程序启用了 ENABLE_VIRTUAL_TERMINAL_PROCESSING（如 vim/less/ncurses），
    // 其 WriteFile 输出已经是 VT 序列，直接转发给 mediator 无需翻译。
    // 避免 VT → ANSI(解码) → W → VT(翻译) 的无意义往返。
    auto& state = ConsoleState::Instance();
    if (state.GetOutputMode() & ENABLE_VIRTUAL_TERMINAL_PROCESSING) {
        SendToMediator(lpBuffer, nNumberOfBytesToWrite);
        if (lpNumberOfBytesWritten != nullptr) {
            *lpNumberOfBytesWritten = nNumberOfBytesToWrite;
        }
        return TRUE;
    }

    // 控制台句柄：按当前输出代码页转 UTF-16，走 WriteConsoleW 翻译路径
    // WriteFile 对控制台的输出是 ANSI 字节流（与 WriteConsoleA 一致）
    UINT cp = ConsoleState::Instance().GetOutputCp();
    int wlen = MultiByteToWideChar(cp, 0, reinterpret_cast<const char*>(lpBuffer),
                                   static_cast<int>(nNumberOfBytesToWrite), nullptr, 0);
    if (wlen > 0) {
        std::wstring wbuf(static_cast<size_t>(wlen), L'\0');
        MultiByteToWideChar(cp, 0, reinterpret_cast<const char*>(lpBuffer),
                            static_cast<int>(nNumberOfBytesToWrite), wbuf.data(), wlen);
        // 复用 WriteConsoleW_Detour 的翻译+发送+光标推进逻辑
        WriteConsoleW_Detour(hFile, wbuf.data(), static_cast<DWORD>(wlen),
                             nullptr, nullptr);
    }

    // Phase 9：不调原 WriteFile，消除原 cmd 黑框闪烁
    // ConHost 不再收到 cmd 输出，原 cmd 窗口停止更新
    if (lpNumberOfBytesWritten != nullptr) {
        *lpNumberOfBytesWritten = nNumberOfBytesToWrite;
    }
    return TRUE;
}

// ============================================================
// Phase 4：SetConsoleTextAttribute Hook
// ============================================================
// 目标程序改变颜色属性时，更新缓存并输出 SGR
// 用途：color 命令、程序主动改变前景/背景色
// Phase 9：不调原 API，避免 ConHost 真改属性导致闪烁
BOOL WINAPI SetConsoleTextAttribute_Detour(HANDLE hConsoleOutput, WORD attr) {
    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        LOG_INFO("SetConsoleTextAttribute_Detour: IsConsoleHandle false, pass-through");
        return SetConsoleTextAttribute_orig(hConsoleOutput, attr);
    }

    LOG_INFO("SetConsoleTextAttribute_Detour: attr=0x%04x", attr);

    // 更新缓存（Phase 14：同时更新 VirtualConsoleState）
    ConsoleState::Instance().SetTextAttribute(attr);
    VirtualConsoleState::Instance().SetAttributes(attr);

    // 输出 SGR（立即生效，下次 WriteConsole 会用新属性）
    std::string sgr = SgrFromAttribute(attr);
    if (!sgr.empty()) {
        SendToMediator(sgr.data(), sgr.size());
    }

    // 不调原 API：ConHost 不再收到属性变更
    return TRUE;
}

// ============================================================
// Phase 4：FillConsoleOutputCharacter Hook
// ============================================================
// 在指定坐标填充 N 个相同字符，用于 cls 清屏
// Phase 9：不调原 API，避免 ConHost 真改导致闪烁
BOOL WINAPI FillConsoleOutputCharacterW_Detour(
    HANDLE hConsoleOutput, wchar_t character, DWORD count,
    COORD writeCoord, LPDWORD lpNumberOfCharsWritten) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return FillConsoleOutputCharacterW_orig(hConsoleOutput, character, count,
                                                writeCoord, lpNumberOfCharsWritten);
    }

    // 翻译为 VT：光标定位 + 字符 + 重复
    std::string vt = ConsoleToVt::FillConsoleOutputCharacter(character, count, writeCoord);
    SendToMediator(vt.data(), vt.size());

    // Phase 10 任务6：cls/填充改变了屏幕内容，失效 WriteConsoleOutput diff 缓存
    ConsoleToVt::InvalidateOutputCache();

    // 注意：FillConsoleOutputCharacter 不改变光标位置（Windows API 语义）
    // 故此处不更新 ConsoleState 的光标缓存

    if (lpNumberOfCharsWritten != nullptr) {
        *lpNumberOfCharsWritten = count;
    }
    // 不调原 API：ConHost 不再收到填充输出
    return TRUE;
}

// A 版本：char → wchar_t 后复用 W 路径
BOOL WINAPI FillConsoleOutputCharacterA_Detour(
    HANDLE hConsoleOutput, char character, DWORD count,
    COORD writeCoord, LPDWORD lpNumberOfCharsWritten) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return FillConsoleOutputCharacterA_orig(hConsoleOutput, character, count,
                                                writeCoord, lpNumberOfCharsWritten);
    }

    // char → wchar_t（按当前代码页）
    wchar_t wch = static_cast<wchar_t>(character);
    return FillConsoleOutputCharacterW_Detour(hConsoleOutput, wch, count,
                                              writeCoord, lpNumberOfCharsWritten);
}

// ============================================================
// Phase 4：FillConsoleOutputAttribute Hook
// ============================================================
// 在指定坐标填充 N 个 cell 的颜色属性，用于 color 命令
// Phase 9：不调原 API，避免 ConHost 真改导致闪烁
BOOL WINAPI FillConsoleOutputAttribute_Detour(
    HANDLE hConsoleOutput, WORD attribute, DWORD count,
    COORD writeCoord, LPDWORD lpNumberOfCharsWritten) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return FillConsoleOutputAttribute_orig(hConsoleOutput, attribute, count,
                                               writeCoord, lpNumberOfCharsWritten);
    }

    // 翻译为 VT：光标定位 + SGR
    std::string vt = ConsoleToVt::FillConsoleOutputAttribute(attribute, count, writeCoord);
    SendToMediator(vt.data(), vt.size());

    // Phase 10 任务6：颜色填充改变了屏幕属性，失效 WriteConsoleOutput diff 缓存
    ConsoleToVt::InvalidateOutputCache();

    if (lpNumberOfCharsWritten != nullptr) {
        *lpNumberOfCharsWritten = count;
    }
    // 不调原 API：ConHost 不再收到属性填充
    return TRUE;
}

// ============================================================
// Phase 4：WriteConsoleOutput Hook
// ============================================================
// 写字符矩阵（每个 cell 带字符+属性），用于全屏重绘
// Phase 9：不调原 API，避免 ConHost 真改导致闪烁
BOOL WINAPI WriteConsoleOutputW_Detour(
    HANDLE hConsoleOutput, const CHAR_INFO* buffer,
    COORD bufferSize, COORD bufferCoord, PSMALL_RECT writeRegion) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput) || buffer == nullptr || writeRegion == nullptr) {
        return WriteConsoleOutputW_orig(hConsoleOutput, buffer, bufferSize,
                                        bufferCoord, writeRegion);
    }

    // 翻译为 VT：逐 cell 光标定位 + SGR + 字符
    std::string vt = ConsoleToVt::WriteConsoleOutput(
        buffer, bufferSize, bufferCoord, *writeRegion);
    SendToMediator(vt.data(), vt.size());

    // 不调原 API：ConHost 不再收到矩阵输出
    return TRUE;
}

// A 版本：CHAR_INFO 中 char 字段是 char，需转 wchar_t
// 简化实现：先调原 A API 让 ConHost 处理，同时把 char 当 wchar_t 走翻译
// （char 字符在 0-127 范围内与 wchar_t 一致，超出范围按当前 CP 转换）
// Phase 9：不调原 API（A→W 转换后复用 W 路径翻译，ConHost 不参与）
BOOL WINAPI WriteConsoleOutputA_Detour(
    HANDLE hConsoleOutput, const CHAR_INFO* buffer,
    COORD bufferSize, COORD bufferCoord, PSMALL_RECT writeRegion) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput) || buffer == nullptr || writeRegion == nullptr) {
        return WriteConsoleOutputA_orig(hConsoleOutput, buffer, bufferSize,
                                        bufferCoord, writeRegion);
    }

    // A → W：把 CHAR_INFO 数组中的 char 转成 wchar_t
    // 分配临时 W 缓冲区
    int cellCount = bufferSize.X * bufferSize.Y;
    std::vector<CHAR_INFO> wbuf(static_cast<size_t>(cellCount));
    UINT cp = ConsoleState::Instance().GetOutputCp();

    for (int i = 0; i < cellCount; ++i) {
        wbuf[i] = buffer[i];
        // char → wchar_t 转换（单个字符）
        char ch = static_cast<char>(buffer[i].Char.AsciiChar);
        wchar_t wch = static_cast<wchar_t>(ch);
        if (ch & 0x80) {
            // 高位字符：按当前 CP 转换
            MultiByteToWideChar(cp, 0, &ch, 1, &wch, 1);
        }
        wbuf[i].Char.UnicodeChar = wch;
    }

    // 复用 W 翻译路径
    std::string vt = ConsoleToVt::WriteConsoleOutput(
        wbuf.data(), bufferSize, bufferCoord, *writeRegion);
    SendToMediator(vt.data(), vt.size());

    // 不调原 API：ConHost 不再收到矩阵输出
    return TRUE;
}

// ============================================================
// Phase 4：WriteConsoleOutputCharacter Hook
// ============================================================
// 在指定坐标写一串字符（不改颜色），用于 prompt 等
// Phase 9：不调原 API，避免 ConHost 真改导致闪烁
BOOL WINAPI WriteConsoleOutputCharacterW_Detour(
    HANDLE hConsoleOutput, const wchar_t* buffer, DWORD length,
    COORD writeCoord, LPDWORD lpNumberOfCharsWritten) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return WriteConsoleOutputCharacterW_orig(hConsoleOutput, buffer, length,
                                                 writeCoord, lpNumberOfCharsWritten);
    }

    // 翻译为 VT：光标定位 + UTF-8 字符串
    std::string vt = ConsoleToVt::WriteConsoleOutputCharacter(buffer, length, writeCoord);
    SendToMediator(vt.data(), vt.size());

    // Phase 10 任务6：局部文本覆盖改变了屏幕内容，失效 WriteConsoleOutput diff 缓存
    ConsoleToVt::InvalidateOutputCache();

    // 注意：WriteConsoleOutputCharacter 不改变光标位置（Windows API 语义）
    // 故此处不更新 ConsoleState 的光标缓存

    if (lpNumberOfCharsWritten != nullptr) {
        *lpNumberOfCharsWritten = length;
    }
    // 不调原 API：ConHost 不再收到字符输出
    return TRUE;
}

// A 版本：char[] → wchar_t[] 后复用 W 路径
BOOL WINAPI WriteConsoleOutputCharacterA_Detour(
    HANDLE hConsoleOutput, const char* buffer, DWORD length,
    COORD writeCoord, LPDWORD lpNumberOfCharsWritten) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput)) {
        return WriteConsoleOutputCharacterA_orig(hConsoleOutput, buffer, length,
                                                 writeCoord, lpNumberOfCharsWritten);
    }

    // char[] → wchar_t[]（按当前代码页）
    UINT cp = ConsoleState::Instance().GetOutputCp();
    int wlen = MultiByteToWideChar(cp, 0, buffer, static_cast<int>(length), nullptr, 0);
    if (wlen <= 0) {
        return WriteConsoleOutputCharacterA_orig(hConsoleOutput, buffer, length,
                                                 writeCoord, lpNumberOfCharsWritten);
    }
    std::wstring wbuf(static_cast<size_t>(wlen), L'\0');
    MultiByteToWideChar(cp, 0, buffer, static_cast<int>(length), wbuf.data(), wlen);

    return WriteConsoleOutputCharacterW_Detour(hConsoleOutput, wbuf.data(),
                                               static_cast<DWORD>(wlen),
                                               writeCoord, lpNumberOfCharsWritten);
}

// ============================================================
// Phase 4：ScrollConsoleScreenBuffer Hook
// ============================================================
// 滚动屏幕缓冲区区域，用于滚屏
// Phase 9：不调原 API，避免 ConHost 真改导致闪烁
BOOL WINAPI ScrollConsoleScreenBufferW_Detour(
    HANDLE hConsoleOutput, const SMALL_RECT* lpScrollRect,
    const SMALL_RECT* lpClipRect, COORD dwDestinationOrigin,
    const CHAR_INFO* lpFill) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput) || lpScrollRect == nullptr || lpFill == nullptr) {
        return ScrollConsoleScreenBufferW_orig(hConsoleOutput, lpScrollRect, lpClipRect,
                                               dwDestinationOrigin, lpFill);
    }

    // 翻译为 VT：清屏式滚动检测 + 普通滚屏
    std::string vt = ConsoleToVt::ScrollConsoleScreenBuffer(
        *lpScrollRect, lpClipRect, dwDestinationOrigin,
        lpFill->Char.UnicodeChar, lpFill->Attributes);
    SendToMediator(vt.data(), vt.size());

    // Phase 10 任务6：滚屏使屏幕 cell 偏移，失效 WriteConsoleOutput diff 缓存
    ConsoleToVt::InvalidateOutputCache();

    // 不调原 API：ConHost 不再收到滚屏
    return TRUE;
}

// A 版本：CHAR_INFO 中 char → wchar_t 后复用 W 路径
// Phase 9：不调原 API（A→W 转换后复用 W 路径翻译，ConHost 不参与）
BOOL WINAPI ScrollConsoleScreenBufferA_Detour(
    HANDLE hConsoleOutput, const SMALL_RECT* lpScrollRect,
    const SMALL_RECT* lpClipRect, COORD dwDestinationOrigin,
    const CHAR_INFO* lpFill) {

    ENSURE_INITIALIZED();
    HookReentryGuard guard;

    if (!IsConsoleHandle(hConsoleOutput) || lpScrollRect == nullptr || lpFill == nullptr) {
        return ScrollConsoleScreenBufferA_orig(hConsoleOutput, lpScrollRect, lpClipRect,
                                               dwDestinationOrigin, lpFill);
    }

    // 把 CHAR_INFO 的 char 转 wchar_t
    CHAR_INFO wFill = *lpFill;
    char ch = static_cast<char>(lpFill->Char.AsciiChar);
    wchar_t wch = static_cast<wchar_t>(ch);
    if (ch & 0x80) {
        UINT cp = ConsoleState::Instance().GetOutputCp();
        MultiByteToWideChar(cp, 0, &ch, 1, &wch, 1);
    }
    wFill.Char.UnicodeChar = wch;

    std::string vt = ConsoleToVt::ScrollConsoleScreenBuffer(
        *lpScrollRect, lpClipRect, dwDestinationOrigin,
        wFill.Char.UnicodeChar, wFill.Attributes);
    SendToMediator(vt.data(), vt.size());

    // Phase 10 任务6：滚屏使屏幕 cell 偏移，失效 WriteConsoleOutput diff 缓存
    ConsoleToVt::InvalidateOutputCache();

    // 不调原 API：ConHost 不再收到滚屏
    return TRUE;
}

// ============================================================
// 注册所有输出类 Hook
// ============================================================
void RegisterOutputHooks() {
    // 优先从 kernelbase.dll 获取函数地址，回退到 kernel32.dll
    //
    // 原因：现代 Windows 的 API Set（如 api-ms-win-core-kernel32-legacy-l1-1-0.dll）
    // 解析到 kernelbase.dll 而非 kernel32.dll。cmd.exe 等程序通过 API Set 导入，
    // IAT 直接指向 kernelbase 的实现。若只 Hook kernel32 的桩函数（kernel32 内部
    // 转发到 kernelbase），则目标进程的实际调用绕过 Hook，Detour 永不触发。
    //
    // Hook kernelbase 的实现可同时拦截两条调用路径：
    //   1. API Set → kernelbase!WriteConsoleW（cmd.exe 等程序的直接路径）
    //   2. kernel32!WriteConsoleW → kernelbase!WriteConsoleW（旧式导入路径）
    // 且不会双重递归：Detour 内调 *_orig（trampoline）执行 kernelbase 原始代码，
    // 不再经过 kernelbase 入口。
    HMODULE hKBase = GetModuleHandleW(L"kernelbase.dll");
    HMODULE hK32   = GetModuleHandleW(L"kernel32.dll");

    // 辅助 lambda：优先 kernelbase，回退 kernel32
    auto resolve = [hKBase, hK32](const char* name) -> void* {
        if (hKBase != nullptr) {
            void* p = GetProcAddress(hKBase, name);
            if (p != nullptr) return p;
        }
        if (hK32 != nullptr) {
            return GetProcAddress(hK32, name);
        }
        return nullptr;
    };

    // 收集所有 Hook 条目
    std::vector<HookEntry> entries;

    // Phase 3：文本流输出
    entries.push_back({"WriteConsoleW",
        resolve("WriteConsoleW"),
        reinterpret_cast<void*>(&WriteConsoleW_Detour),
        reinterpret_cast<void**>(&WriteConsoleW_orig)});
    entries.push_back({"WriteConsoleA",
        resolve("WriteConsoleA"),
        reinterpret_cast<void*>(&WriteConsoleA_Detour),
        reinterpret_cast<void**>(&WriteConsoleA_orig)});
    entries.push_back({"WriteFile",
        resolve("WriteFile"),
        reinterpret_cast<void*>(&WriteFile_Detour),
        reinterpret_cast<void**>(&WriteFile_orig)});

    // Phase 4：属性设置
    entries.push_back({"SetConsoleTextAttribute",
        resolve("SetConsoleTextAttribute"),
        reinterpret_cast<void*>(&SetConsoleTextAttribute_Detour),
        reinterpret_cast<void**>(&SetConsoleTextAttribute_orig)});

    // Phase 4：填充输出
    entries.push_back({"FillConsoleOutputCharacterW",
        resolve("FillConsoleOutputCharacterW"),
        reinterpret_cast<void*>(&FillConsoleOutputCharacterW_Detour),
        reinterpret_cast<void**>(&FillConsoleOutputCharacterW_orig)});
    entries.push_back({"FillConsoleOutputCharacterA",
        resolve("FillConsoleOutputCharacterA"),
        reinterpret_cast<void*>(&FillConsoleOutputCharacterA_Detour),
        reinterpret_cast<void**>(&FillConsoleOutputCharacterA_orig)});
    entries.push_back({"FillConsoleOutputAttribute",
        resolve("FillConsoleOutputAttribute"),
        reinterpret_cast<void*>(&FillConsoleOutputAttribute_Detour),
        reinterpret_cast<void**>(&FillConsoleOutputAttribute_orig)});

    // Phase 4：矩阵输出
    entries.push_back({"WriteConsoleOutputW",
        resolve("WriteConsoleOutputW"),
        reinterpret_cast<void*>(&WriteConsoleOutputW_Detour),
        reinterpret_cast<void**>(&WriteConsoleOutputW_orig)});
    entries.push_back({"WriteConsoleOutputA",
        resolve("WriteConsoleOutputA"),
        reinterpret_cast<void*>(&WriteConsoleOutputA_Detour),
        reinterpret_cast<void**>(&WriteConsoleOutputA_orig)});
    entries.push_back({"WriteConsoleOutputCharacterW",
        resolve("WriteConsoleOutputCharacterW"),
        reinterpret_cast<void*>(&WriteConsoleOutputCharacterW_Detour),
        reinterpret_cast<void**>(&WriteConsoleOutputCharacterW_orig)});
    entries.push_back({"WriteConsoleOutputCharacterA",
        resolve("WriteConsoleOutputCharacterA"),
        reinterpret_cast<void*>(&WriteConsoleOutputCharacterA_Detour),
        reinterpret_cast<void**>(&WriteConsoleOutputCharacterA_orig)});

    // Phase 4：滚屏
    entries.push_back({"ScrollConsoleScreenBufferW",
        resolve("ScrollConsoleScreenBufferW"),
        reinterpret_cast<void*>(&ScrollConsoleScreenBufferW_Detour),
        reinterpret_cast<void**>(&ScrollConsoleScreenBufferW_orig)});
    entries.push_back({"ScrollConsoleScreenBufferA",
        resolve("ScrollConsoleScreenBufferA"),
        reinterpret_cast<void*>(&ScrollConsoleScreenBufferA_Detour),
        reinterpret_cast<void**>(&ScrollConsoleScreenBufferA_orig)});

    // 检查所有地址已解析
    for (const auto& e : entries) {
        if (e.target == nullptr) {
            LOG_ERROR("RegisterOutputHooks: failed to resolve %s", e.name);
            return;
        }
    }

    HookManager::RegisterBatch(entries);
    LOG_INFO("OutputHooks registered (%zu hooks)", entries.size());
}

} // namespace terminjector::hooks
