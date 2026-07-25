// DLL 主动卸载器（Phase 11）
// 详见 docs/phases/11-unload-testing.md 4.2
//
// 设计要点：
//   - 管道断开或收到 Shutdown 消息时，DllRecvLoop 调 RequestUnload 触发主动卸载
//   - 不依赖 DLL_PROCESS_DETACH（进程退出才触发），实现"WT tab 关闭 → DLL 即时卸载"
//   - 在独立线程执行卸载，避免在 recv 线程或 Hook 线程中死锁
//   - FreeLibraryAndExitThread 是唯一安全的自卸载方式：
//       1) 减少 DLL 引用计数
//       2) 触发 DLL_PROCESS_DETACH（DllMain 做最终清理）
//       3) 终止当前线程
//
// 与 DllMain DETACH 的协作：
//   - s_unloading 标志让 DETACH 知道是 Unloader 触发的（跳过重复清理）
//   - 进程退出触发的 DETACH（s_unloading=false）仍做完整清理
//   - UninstallAll / Stop / Shutdown 等方法本身幂等，重复调用安全
//
// Read 类 Detour 线程离开等待（2026-07-25 修复 cmd 崩溃）：
//   - cmd 主线程阻塞在 ReadConsoleW_Detour 的 WaitForSingleObject
//   - Unloader SignalDataReady 唤醒后立即 UninstallAll 会释放 trampoline
//   - 主线程被调度回来调用 ReadConsoleW_orig（trampoline）→ AV 0xC0000005
//   - 修复：UninstallAll 前等待 s_active_read_detours 归零
//   - Detour 入口 EnterReadDetour（计数++），pass-through 调 orig 前 LeaveReadDetour（计数--）
//   - 正常返回路径由 ReadDetourGuard RAII 析构时 --
#pragma once

#include <atomic>

namespace terminjector {

class Unloader {
public:
    // 请求卸载（可从任意线程调用，线程安全）
    // 多次调用只有首次会真正执行卸载，其余直接返回
    static void RequestUnload();

    // 是否正在卸载（Hook Detour 可据此跳过拦截，避免卸载过程中再触发 Hook 逻辑）
    static bool IsUnloading() { return s_unloading.load(); }

    // Read 类 Detour 进入/离开计数（DoUnload 在 UninstallAll 前等待归零）
    // 关键：MinHook 的 MH_RemoveHook 释放 trampoline 内存，若此时有线程在
    //       Detour 内调用 *_orig（trampoline），会访问已释放内存 → AV
    //       必须等所有 Read 类 Detour 线程离开 DLL 代码后再 RemoveHook
    static void EnterReadDetour() { s_active_read_detours.fetch_add(1); }
    static void LeaveReadDetour() { s_active_read_detours.fetch_sub(1); }
    static int  ActiveReadDetours() { return s_active_read_detours.load(); }

private:
    static void DoUnload();
    static std::atomic<bool> s_unloading;
    static std::atomic<int>  s_active_read_detours;
};

} // namespace terminjector
