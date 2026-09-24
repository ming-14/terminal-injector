// 同位数远程注入原语（Phase 23）
//
// 被两处共用（避免各写一份而走样）：
//   - relay32.dll      ：把 relay32.dll 注入自己拉起的 32 位子进程
//   - relay32inject.exe：把 relay32.dll 注入 x64 injected.dll 冻住的 32 位子进程
//
// 为什么必须同位数：
//   远程 LoadLibraryW 的入口地址取自【本进程】的 kernel32。kernel32 是
//   KnownDLL —— 同一 boot 内所有同位数进程共享同一基址，所以该地址在
//   同位数目标内有效。位数不同则地址无意义：实测 64 位侧向 32 位目标注入
//   LoadLibraryW 返回 0（线程起来了，但加载失败）。这就是这套注入必须
//   跑在 32 位进程里的唯一原因。
//
// 为什么对 CREATE_SUSPENDED 的目标也能用：
//   挂起进程的 loader 尚未初始化，模块表为空、kernel32 还没映射
//   （实测挂起态只剩 exe + ntdll 两个映像）。但 KnownDLL 的基址是 boot 内
//   固定的：远程线程启动会自行触发 loader 初始化，等它执行到入口时
//   kernel32 已经在那个固定基址上就位。
//   实测：向挂起的 32 位进程注入成功且 loaded 标记 0ms 落地，无需目标先跑起来。
#pragma once

#include <windows.h>

#include <cstdint>
#include <string>

namespace terminjector::relay32 {

// 向同位数目标进程注入 dllPath，并跨进程调用其 RemotePipeSetup 下发管道参数。
//
// hProcess     目标进程句柄（需 PROCESS_CREATE_THREAD | VM_OPERATION | VM_WRITE | VM_READ）
// targetPid    目标 PID（仅用于日志）
// dllPath      要注入的 DLL 全路径（须与本机文件一致，RemoteCallExport 要按它算导出 RVA）
// pipeName     会话管道名（随 PipeParams 下发；名字由调用方生成，两侧必须一致）
// mediatorPid  管道服务端进程 PID（目标内 DLL 据此校验服务端身份；0=跳过校验）
//
// 返回 true 表示 DLL 已在目标内加载完成（DllMain 已返回）且管道参数已下发。
bool InjectDllSameBitness(HANDLE hProcess, uint32_t targetPid,
                          const std::wstring& dllPath,
                          const std::wstring& pipeName,
                          uint32_t mediatorPid);

} // namespace terminjector::relay32
