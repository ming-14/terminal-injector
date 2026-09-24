// 目标进程位数判定（跨位数注入的公共前提）
//
// 为什么不用 IsWow64Process：
//   它的语义随【调用方】位数而变。本项目两侧都要判子进程位数：
//     64 位 injected.dll 判子进程 → IsWow64Process 返回 TRUE 表示"目标是 32 位"
//     32 位 relay32.dll  判子进程 → 它自己就是 WOW64 进程，同样调用会返回
//                                   FALSE（"我自己不是 WOW64"），语义完全反了
//   而 GetBinaryTypeW 只看镜像文件本身，与调用方位数无关，两边读到的含义一致。
//
// 为什么不用目标进程的模块表：
//   子进程被冻在 CREATE_SUSPENDED 时 loader 尚未初始化，模块表为空
//   （实测 EnumProcessModulesEx 返回 err=299），根本拿不到模块列表。
//   QueryFullProcessImageNameW + GetBinaryTypeW 不依赖目标 loader，挂起态可用。
#pragma once

#include <windows.h>

#include <iterator>

namespace terminjector {

enum class ProcessBitness { Unknown, X86, X64 };

// 用镜像类型判定进程位数。失败（拿不到镜像路径/类型异常）返回 Unknown。
inline ProcessBitness QueryProcessBitness(HANDLE hProcess) {
    wchar_t path[1024] = {0};
    DWORD len = static_cast<DWORD>(std::size(path));
    if (!QueryFullProcessImageNameW(hProcess, 0, path, &len)) {
        return ProcessBitness::Unknown;
    }
    DWORD type = 0;
    if (!GetBinaryTypeW(path, &type)) {
        return ProcessBitness::Unknown;
    }
    if (type == SCS_32BIT_BINARY) {
        return ProcessBitness::X86;
    }
    if (type == SCS_64BIT_BINARY) {
        return ProcessBitness::X64;
    }
    return ProcessBitness::Unknown;
}

} // namespace terminjector
