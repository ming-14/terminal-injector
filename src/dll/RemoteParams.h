// 注入参数存取（DLL 侧）
// 详见 docs/phases/02-injector-modes.md 4.4（管道参数传递，安全审查 HIGH #2）
//
// 职责：
//   - 导出 RemotePipeSetup：注入器/父 DLL 通过 RemoteCall 跨进程调用，
//     把 PipeParams（随机管道名 + mediatorPid）写入本进程全局
//   - GetPipeParams：LazyInit / ProcessHooks 查询参数是否就绪并拷贝
//
// 线程安全：参数写入由注入器远程线程执行（非 DllMain 期，无 Loader Lock）；
// 就绪标志用 InterlockedExchange 原子写，读者用原子读。
#pragma once

#include "transport/PipeParams.h"

namespace terminjector {

// 查询注入参数是否就绪，就绪则拷贝到 out 返回 true
// 未就绪（注入器 RemotePipeSetup 尚未执行）返回 false
bool GetPipeParams(PipeParams& out);

} // namespace terminjector