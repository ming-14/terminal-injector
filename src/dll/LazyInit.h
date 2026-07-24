// 懒加载初始化守卫
// 详见 docs/phases/03-dll-framework.md 4.1
//
// 设计要点：
//   - 双检锁 + InterlockedCompareExchange 避免重入
//   - 首个 Hook Detour 入口调用 EnsureLazyInitialized()
//   - 失败时仍标记已初始化，Hook 走 pass-through（调原 API）
//
// 触发器说明（偏离文档 4.1 的合理调整）：
//   文档设想 EnsureLazyInitialized 内调 HookManager::InstallAll，
//   但 Hook 未启用则 Detour 不触发，懒加载无法启动（鸡生蛋）。
//   故改为：DllMain 里 Register + InstallAll（启用 Hook 提供触发器），
//   EnsureLazyInitialized 只做 Logger/Capture/Connect/State 初始化。
#pragma once

namespace terminjector {

class ITransport;  // 前向声明，避免头文件依赖

// 懒加载初始化入口（首个 Hook Detour 调用）
// 线程安全，仅一次执行；失败时 Hook 走 pass-through
void EnsureLazyInitialized();

// 当前线程是否正在懒加载初始化中
// Hook Detour 用此判断是否跳过拦截（避免 Logger 写日志触发 WriteFile Hook 死锁）
bool IsInLazyInit();

// 获取 mediator 传输通道（供 Hook 内 SendToMediator 使用）
// 未连接时返回 nullptr
ITransport* GetMediatorTransport();

// 是否已完成懒加载初始化
bool IsLazyInitialized();

// 本 DLL 实例所在进程是否为注入目标进程（mediator 主会话）
// 由 HelloAck.isTarget 决定：true=注入目标（需 KickStart），false=子进程
// LazyInit 完成前返回 false
bool IsTargetProcess();

// 释放 mediator 传输通道（DLL_PROCESS_DETACH 调用）
void ReleaseMediatorTransport();

} // namespace terminjector
