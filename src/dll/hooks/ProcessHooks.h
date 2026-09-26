// 进程创建类 API Hook 声明
// 详见 docs/phases/12-child-process-injection.md 4.2
//
// Phase 12：拦截 CreateProcessW/A，自动注入 DLL 到子进程
//   - 强制 CREATE_SUSPENDED → 注入 DLL → ResumeThread
//   - 发送 ChildProcessNotify 通知 mediator 创建子进程管道实例
//   - 子进程 DLL 通过 GetCurrentProcessId() 自发现管道名，无需环境变量
#pragma once

namespace terminjector::hooks {

// 注册进程创建类 Hook（由 DllMain 调用）
void RegisterProcessHooks();

// 接管「注入前就已存在于同一控制台」的后代进程。
//
// CreateProcess Hook 只能覆盖注入【之后】新起的子进程；注入前已在运行的后代
// （典型：WT 的 shell 里先跑起了 TUI，再劫持该 shell）永远进不了接管链路 ——
// 它的输出仍写旧 ConHost，新 WT 只停在注入瞬间重放的一帧（用户报"画面不刷新、
// 无法输入"）。本函数枚举同控制台进程，把父链能回溯到本进程的后代逐个按
// CreateProcess 捕获路径同一套流程接管（通知 mediator 建子会话 + 按位数注入）。
//
// 仅对注入目标进程（IsTargetProcess）生效且只执行一次：后代自己是子会话
// （isTarget=0），不会重复接管；否则后代反向枚举祖先会互相注入成环。
// 按后代深度升序注入 —— mediator 的 RouteInput 取「最后加入的活跃子会话」为
// 前台，最深者（真正的 TUI）必须最后加入才拿得到输入。
// 由 DllMain 的懒加载 worker 在 KickStart 之后调用（见 dllmain.cpp）。
void AdoptConsoleDescendants();

} // namespace terminjector::hooks
