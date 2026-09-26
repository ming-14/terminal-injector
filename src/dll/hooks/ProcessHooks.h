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

} // namespace terminjector::hooks
