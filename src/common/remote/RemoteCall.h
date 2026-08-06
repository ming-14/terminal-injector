// 跨进程调用已加载 DLL 的导出函数并传参（RemoteCall）
// 详见 docs/phases/02-injector-modes.md 4.4（管道参数传递）
//
// 背景：
//   CreateRemoteThread + LoadLibraryW 的注入方式无法直接传参给 DLL。
//   本模块提供通用机制：DLL 在目标进程加载完成后，跨进程调用其导出
//   函数并传入一段参数数据（如 PipeParams）。
//
// 原理（x64，两阶段）：
//   1. 解析导出地址：本地 LoadLibraryExW(DONT_RESOLVE_DLL_REFERENCES)
//      加载 DLL 副本（不执行 DllMain），本地 GetProcAddress 拿到导出
//      函数，减去本地基址得 RVA；目标进程中地址 = hRemoteDll + RVA。
//      不采用"远程 GetProcAddress stub"：其一 GetExitCodeThread 只有
//      32 位会截断 64 位指针；其二远程自定义页 stub 会因目标进程的
//      CFG/远程线程防护随机崩溃（0xC0000005），且远程线程数受限制，
//      第 3 个远程线程会被拒绝（ERROR_ACCESS_DENIED）。
//   2. 远程分配参数块，CreateRemoteThread(导出地址, 参数块) 调用导出函数。
//   3. 线程退出码即导出函数返回值（约定返回 BOOL）。
//
// 线程安全：每次远程线程都会等待完成，调用方串行即可。
#pragma once

#include <windows.h>
#include <cstdlib>
#include <cstdint>
#include <string>

namespace terminjector {

// 跨进程调用目标进程中已加载的 DLL 导出函数，并传入参数
// hProcess   目标进程句柄（需 PROCESS_CREATE_THREAD | VM_* 权限）
// hRemoteDll 目标进程中已加载的 DLL HMODULE（完整 64 位基址）
// dllPath    本机 DLL 文件路径（本地解析导出 RVA 用，须与远程模块同一文件）
// exportName 导出函数名（ASCII，如 "RemotePipeSetup"）
// param      传给导出函数的参数缓冲区（可为 nullptr）
// paramSize  参数缓冲区字节数
// outRet     可选，接收远程导出函数返回值
// 返回 true 表示远程函数被调用且返回非 0（约定导出函数返回 BOOL）
bool RemoteCallExport(HANDLE hProcess, HMODULE hRemoteDll,
                      const std::wstring& dllPath,
                      const char* exportName,
                      const void* param, size_t paramSize,
                      uintptr_t* outRet = nullptr);

} // namespace terminjector
