// injected.dll 导出函数声明
// 详见 docs/phases/02-injector-modes.md 4.6
//
// 导出函数命名约定：
//   Inject_<动作>：所有导出函数以 Inject_ 前缀，便于在 DumpBin / 注入器中识别
//
// Phase 2 仅导出 Inject_QueryVersion，用于：
//   1. 验证 DLL 是我们的 injected.dll（注入器/mediator 可远程调用查版本）
//   2. 后续 Phase 兼容性检查（DLL 版本与 mediator 期望版本匹配）
//
// Phase 15（安全审查 HIGH #2）：导出 RemotePipeSetup
//   注入器/父 DLL 经 RemoteCallExport 跨进程调用，传入 PipeParams
//   （随机管道名 + mediatorPid）。旧方案 DLL 用 pid 约定自发现管道名，
//   名字可预测易被抢占；现在名字由服务端生成随机后缀经此函数下发。
#pragma once

#include <cstdint>

#include "transport/PipeParams.h"

#ifdef __cplusplus
extern "C" {
#endif

// 查询 DLL 版本号
// 返回：静态字符串指针（无需释放），格式 "主.次.修订"，如 "0.1.0"
// 注意：函数签名固定为 void -> const char*，便于跨 CRT 远程调用
__declspec(dllexport) const char* Inject_QueryVersion(void);

#ifdef __cplusplus
} // extern "C"
#endif
