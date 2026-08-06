// RemotePipeSetup：跨进程注入参数接收
// 详见 RemoteParams.h 头注释
//
// 注意：DllMain 期间 Logger 可能未初始化，Log 宏依赖 Logger 单例。
// 本函数由注入器远程线程调用（Loader Lock 已释放），
// 若 LazyInit 已完成（Logger 就绪）则写日志，否则只写 OutputDebugStringW。
#include "RemoteParams.h"
#include "logging/Logger.h"

#include <windows.h>

namespace terminjector {

namespace {

// 注入参数全局 + 就绪原子标志
PipeParams g_pipeParams{};          // 初始化为全 0（mediatorPid=0=跳过校验）
volatile LONG g_pipeParamsReady = 0;

} // namespace

// 注入器/父 DLL 注入后经 RemoteCallExport 远程调用（目标进程内执行）
// 参数指针在目标进程地址空间（注入器远程分配并写入）
extern "C" __declspec(dllexport) BOOL WINAPI RemotePipeSetup(const PipeParams* p) {
    if (p == nullptr) {
        return FALSE;
    }
    g_pipeParams = *p;
    InterlockedExchange(&g_pipeParamsReady, 1);
    if (Logger::IsInitialized()) {
        LOG_INFO("RemotePipeSetup installed: pipe=%ls mediatorPid=%u",
                 g_pipeParams.pipeName, g_pipeParams.mediatorPid);
    }
    OutputDebugStringW(L"[terminjector] RemotePipeSetup installed");
    return TRUE;
}

bool GetPipeParams(PipeParams& out) {
    if (InterlockedCompareExchange(&g_pipeParamsReady, 0, 0) == 0) {
        return false;
    }
    out = g_pipeParams;
    return true;
}

} // namespace terminjector