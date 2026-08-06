// 注入参数：管道连接所需信息（注入器 -> 目标进程 DLL）
// 详见 docs/phases/01-scaffold.md 4.4.2（管道安全加固）
//
// 背景（安全审查 HIGH #2）：
//   旧方案 DLL 用 GetCurrentProcessId() + MakePipeName 自发现管道名，
//   管道名完全可预测，同会话任意进程可预先 CreateNamedPipeW 抢占，
//   把注入 DLL 连到伪造 mediator（管道承载键盘输入与屏幕内容）。
//
// 加固方案（完整，非缓解）：
//   1. 管道名由服务端生成随机后缀，注入器经远程调用 RemotePipeSetup
//      传给 DLL（CreateRemoteThread+LoadLibraryW 无参数通道，需远程
//      GetProcAddress 后调用导出函数传参，见 RemoteCall）
//   2. DLL 连接后 GetNamedPipeServerProcessId 校验服务端进程
//      == mediatorPid，不一致立即断开（防中间人/抢占）
//   3. CreateNamedPipeW 用当前用户 SID 的 DACL（防跨用户访问）
//
// 子进程链路：父进程 DLL 的 ProcessHooks 注入子 DLL 时同样传参，
// mediatorPid 继承自父 DLL（同一 mediator）。
#pragma once

#include <windows.h>
#include <cstdint>

namespace terminjector {

// 最大管道名长度（含 null 终止符）
constexpr size_t kMaxPipeNameLen = 128;

// DLL 注入参数（注入器/父 DLL 写入，目标进程 RemotePipeSetup 保存）
struct PipeParams {
    wchar_t pipeName[kMaxPipeNameLen];  // \\.\pipe\terminjector_<pid>_<hex16>
    uint32_t mediatorPid;               // 管道服务端进程 PID（校验用，0=跳过校验）
};

static_assert(sizeof(PipeParams) == kMaxPipeNameLen * sizeof(wchar_t) + sizeof(uint32_t),
              "PipeParams 大小应与预期一致");

} // namespace terminjector
