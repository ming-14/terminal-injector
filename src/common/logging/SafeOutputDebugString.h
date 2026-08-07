// SafeOutputDebugStringW：带重入保护的 OutputDebugStringW
//
// 崩溃根因（2026-08-07 修复，dump 栈证据）：
//   OutputDebugStringW 在无调试器时走 DBWIN 协议，kernelbase 内部会
//   OpenEvent / WaitForSingleObjectEx / CloseHandle 等 DBWIN 事件句柄。
//   我们 Hook 了 WaitForSingleObjectEx 与 CloseHandle，因此任意 Hook
//   Detour 内调 ODS（直接调用或经 Logger::LogV）都会在 ODS 内部重入
//   被 Hook 的 API → Detour 再调 ODS → 无限递归 → 栈耗尽 → __chkstk
//   溢出 0xC00000FD。历史崩溃 dump 栈的两种形态：
//     WaitForSingleObjectEx_Detour → EnsureLazyInitialized → ODS → ODS(DBWIN
//     等待) → WaitForSingleObjectEx → Detour → ...
//     CloseHandle_Detour → Logger::LogV → ODS → ODS → CloseHandle → Detour → ...
//
// 方案：thread_local 标志，ODS 调用期间置位；重入（标志已置）时直接跳过
//   输出。递归在第一个 ODS 出口即被截断（嵌套深度=2），对 CloseHandle /
//   WaitForSingleObjectEx 及未来新增的任何被 Hook API 的链一刀切。
//   注意：ODS 是 C 函数不会抛异常，无 RAII 需求；标志复位必须紧随调用。
#pragma once

#include <windows.h>

namespace terminjector {

// 当前线程是否正处于 OutputDebugStringW 调用中
inline thread_local bool t_inSafeOds = false;

inline void SafeOutputDebugStringW(const wchar_t* text) noexcept {
    if (t_inSafeOds) {
        return;  // 重入：已处于 ODS（ODS 内部 DBWIN 操作被 Hook 重入），跳过
    }
    t_inSafeOds = true;
    ::OutputDebugStringW(text);
    t_inSafeOds = false;
}

} // namespace terminjector
