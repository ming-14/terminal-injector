// ProcessHelper 实现
// 详见 docs/phases/02-injector-modes.md 4.4.3
#include "ProcessHelper.h"
#include "../common/logging/Logger.h"

#include <tlhelp32.h>
#include <cwctype>
#include <algorithm>

namespace terminjector::ProcessHelper {

bool IsX64Process(HANDLE hProcess) {
    if (!hProcess) return false;

    // IsWow64Process 返回 TRUE 表示进程是 32 位 WoW64
    // 在 x64 Windows 上，返回 FALSE 即为原生 x64 进程
    BOOL isWow64 = FALSE;
    if (!IsWow64Process(hProcess, &isWow64)) {
        LOG_ERROR("IsWow64Process failed: %lu", GetLastError());
        return false;
    }
    return isWow64 == FALSE;
}

bool ToShortPath(const std::wstring& longPath, std::wstring& outShort) {
    if (longPath.empty()) {
        outShort.clear();
        return false;
    }

    // GetShortPathNameW 返回 0 表示失败
    // 首次调用获取所需缓冲长度，第二次实际获取短路径
    DWORD len = GetShortPathNameW(longPath.c_str(), nullptr, 0);
    if (len == 0) {
        LOG_ERROR("GetShortPathNameW(query len) failed: %lu, path=%ls",
                  GetLastError(), longPath.c_str());
        return false;
    }

    outShort.resize(len);
    DWORD written = GetShortPathNameW(longPath.c_str(), outShort.data(), len);
    if (written == 0) {
        LOG_ERROR("GetShortPathNameW failed: %lu, path=%ls",
                  GetLastError(), longPath.c_str());
        return false;
    }
    // 去掉 resize 多余的 null 终止符位置
    outShort.resize(written);
    return true;
}

uint32_t FindProcessByName(const std::wstring& name) {
    if (name.empty()) return 0;

    // 准备小写比较键
    std::wstring lowerName = name;
    std::transform(lowerName.begin(), lowerName.end(), lowerName.begin(),
                   ::towlower);

    HANDLE hSnap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (hSnap == INVALID_HANDLE_VALUE) {
        LOG_ERROR("CreateToolhelp32Snapshot failed: %lu", GetLastError());
        return 0;
    }

    PROCESSENTRY32W pe{};
    pe.dwSize = sizeof(pe);
    uint32_t foundPid = 0;

    if (Process32FirstW(hSnap, &pe)) {
        do {
            std::wstring exeName = pe.szExeFile;
            std::transform(exeName.begin(), exeName.end(), exeName.begin(),
                           ::towlower);
            if (exeName == lowerName) {
                foundPid = pe.th32ProcessID;
                break;
            }
        } while (Process32NextW(hSnap, &pe));
    }

    CloseHandle(hSnap);
    return foundPid;
}

} // namespace terminjector::ProcessHelper
