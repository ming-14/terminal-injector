// ProcessHelper 实现
// 详见 docs/phases/02-injector-modes.md 4.4.3
#include "ProcessHelper.h"
#include "../common/logging/Logger.h"

#include <tlhelp32.h>
#include <psapi.h>   // EnumProcessModulesEx / GetModuleFileNameExW
#include <cwctype>
#include <algorithm>
#include <cstring>  // memcpy（PE 头读取，避免未对齐 reinterpret_cast）
#include <utility>  // std::move

#pragma comment(lib, "psapi.lib")

namespace terminjector::ProcessHelper {

namespace {

// 注入目标进程所需权限（与 Injector::Inject 保持一致）
constexpr DWORD kInjectAccess =
    PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION |
    PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE;

// 局部 RAII 句柄守卫（Injector.cpp 有同名类型，此处独立避免耦合）
struct LocalHandleGuard {
    HANDLE h = nullptr;
    explicit LocalHandleGuard(HANDLE handle) : h(handle) {}
    ~LocalHandleGuard() { if (h && h != INVALID_HANDLE_VALUE) CloseHandle(h); }
    LocalHandleGuard(const LocalHandleGuard&) = delete;
    LocalHandleGuard& operator=(const LocalHandleGuard&) = delete;
};

// 读取 exe 的 PE Subsystem 字段（不依赖目标进程权限，只读文件头）
// 返回：3=WINDOWS_CUI（控制台程序）、2=WINDOWS_GUI、-1=读取失败/未知
int PeSubsystem(const std::wstring& exePath) {
    if (exePath.empty()) return -1;
    HANDLE h = CreateFileW(exePath.c_str(), GENERIC_READ, FILE_SHARE_READ,
                           nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) return -1;
    LocalHandleGuard guard(h);

    BYTE buf[4096];
    DWORD n = 0;
    if (!ReadFile(h, buf, sizeof(buf), &n, nullptr) || n < 0x100) return -1;
    // 栈上 BYTE 数组对齐未定义，字段用 memcpy 安全读取
    DWORD peOff = 0;
    std::memcpy(&peOff, buf + 0x3C, sizeof(peOff));
    if (peOff < 0x40 || peOff + 92 > n) return -1;
    // PE 签名 "PE\0\0" 校验
    DWORD sig = 0;
    std::memcpy(&sig, buf + peOff, sizeof(sig));
    if (sig != 0x00004550) return -1;
    // OptionalHeader 起点 = peOff + 4(签名) + 20(COFF)，Subsystem 在 +68
    WORD subsystem = 0;
    std::memcpy(&subsystem, buf + peOff + 24 + 68, sizeof(subsystem));
    return static_cast<int>(subsystem);
}

// 取进程 exe 完整路径（需 PROCESS_QUERY_LIMITED_INFORMATION 即可）
bool ProcessImagePath(HANDLE hProcess, std::wstring& outPath) {
    wchar_t path[MAX_PATH] = {0};
    DWORD len = MAX_PATH;
    if (!QueryFullProcessImageNameW(hProcess, 0, path, &len)) {
        return false;
    }
    outPath.assign(path, len);
    return true;
}

} // namespace

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

HMODULE FindRemoteModuleByPath(HANDLE hProcess, const std::wstring& name) {
    // 枚举目标进程模块，按文件名（不区分大小写）匹配，返回完整 64 位基址
    // EnumProcessModulesEx 需要 psapi（#pragma comment 链接）
    DWORD cb = 0;
    if (!EnumProcessModulesEx(hProcess, nullptr, 0, &cb,
                              LIST_MODULES_ALL)) {
        LOG_ERROR("FindRemoteModuleByPath: EnumProcessModulesEx(query) failed: %lu",
                  GetLastError());
        return nullptr;
    }
    std::vector<HMODULE> mods(cb / sizeof(HMODULE));
    if (!EnumProcessModulesEx(hProcess, mods.data(), cb, &cb,
                              LIST_MODULES_ALL)) {
        LOG_ERROR("FindRemoteModuleByPath: EnumProcessModulesEx failed: %lu",
                  GetLastError());
        return nullptr;
    }

    std::wstring lower(name);
    std::transform(lower.begin(), lower.end(), lower.begin(),
                   [](wchar_t c) { return static_cast<wchar_t>(::towlower(c)); });

    for (HMODULE hMod : mods) {
        wchar_t path[MAX_PATH] = {0};
        if (GetModuleFileNameExW(hProcess, hMod, path, MAX_PATH) == 0) {
            continue;
        }
        // 取文件名部分，与目标名比对
        std::wstring base(path);
        size_t pos = base.find_last_of(L"\\/");
        std::wstring file = (pos != std::wstring::npos) ? base.substr(pos + 1) : base;
        std::transform(file.begin(), file.end(), file.begin(),
                       [](wchar_t c) { return static_cast<wchar_t>(::towlower(c)); });
        if (file == lower) {
            LOG_INFO("FindRemoteModuleByPath: '%ls' found at %p",
                     name.c_str(), hMod);
            return hMod;
        }
    }
    return nullptr;
}

bool EnableDebugPrivilege() {
    HANDLE hToken = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(),
                          TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY, &hToken)) {
        LOG_ERROR("EnableDebugPrivilege: OpenProcessToken failed: %lu",
                  GetLastError());
        return false;
    }
    LocalHandleGuard tokenGuard(hToken);

    LUID luid{};
    // 注意：SE_DEBUG_NAME 在未定义 UNICODE 时展开为窄字符串，
    // LookupPrivilegeValueW 要求 LPCWSTR，故显式使用 L"SeDebugPrivilege"
    if (!LookupPrivilegeValueW(nullptr, L"SeDebugPrivilege", &luid)) {
        LOG_ERROR("EnableDebugPrivilege: LookupPrivilegeValueW failed: %lu",
                  GetLastError());
        return false;
    }

    TOKEN_PRIVILEGES tp{};
    tp.PrivilegeCount = 1;
    tp.Privileges[0].Luid = luid;
    tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED;

    // AdjustTokenPrivileges 返回 TRUE 不代表权限真正获得，需检查 GetLastError
    if (!AdjustTokenPrivileges(hToken, FALSE, &tp, sizeof(tp), nullptr, nullptr)) {
        LOG_ERROR("EnableDebugPrivilege: AdjustTokenPrivileges failed: %lu",
                  GetLastError());
        return false;
    }
    DWORD err = GetLastError();
    if (err == ERROR_NOT_ALL_ASSIGNED) {
        LOG_WARN("SeDebugPrivilege not assigned (need admin)");
        return false;
    }
    return true;
}

std::vector<InjectTargetInfo> EnumerateInjectTargets() {
    std::vector<InjectTargetInfo> result;

    const uint32_t selfPid = GetCurrentProcessId();
    HANDLE hSnap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (hSnap == INVALID_HANDLE_VALUE) {
        LOG_ERROR("EnumerateInjectTargets: CreateToolhelp32Snapshot failed: %lu",
                  GetLastError());
        return result;
    }

    PROCESSENTRY32W pe{};
    pe.dwSize = sizeof(pe);
    if (Process32FirstW(hSnap, &pe)) {
        do {
            const uint32_t pid = pe.th32ProcessID;
            // 排除系统进程（Idle=0 / System=4）与自身
            if (pid <= 4 || pid == selfPid) continue;

            InjectTargetInfo info;
            info.pid = pid;
            info.name = pe.szExeFile;

            // 1. 权限：以注入所需全套权限打开进程
            HANDLE hProc = OpenProcess(kInjectAccess, FALSE, pid);
            if (!hProc) {
                info.reason = L"access_denied";
                result.push_back(std::move(info));
                continue;
            }
            LocalHandleGuard procGuard(hProc);

            // 2. 架构：仅 x64 可注入
            info.x64 = IsX64Process(hProc);
            if (!info.x64) {
                info.reason = L"not_x64";
                result.push_back(std::move(info));
                continue;
            }

            // 3. 类型：PE Subsystem == CUI 才是控制台程序（注入才有意义）
            //    无控制台/GUI 程序即使注入成功，Hook 的 Console API 也无人调用
            std::wstring exePath;
            if (ProcessImagePath(hProc, exePath)) {
                info.cui = (PeSubsystem(exePath) == 3);
            }
            if (!info.cui) {
                info.reason = L"not_console";
                result.push_back(std::move(info));
                continue;
            }

            // 4. 全部满足 → 可注入；再查是否已注入（重复注入无意义，仅标记）
            info.injectable = true;
            info.alreadyInjected =
                (FindRemoteModuleByPath(hProc, L"injected.dll") != nullptr);

            result.push_back(std::move(info));
        } while (Process32NextW(hSnap, &pe));
    }
    CloseHandle(hSnap);

    std::sort(result.begin(), result.end(),
              [](const InjectTargetInfo& a, const InjectTargetInfo& b) {
                  return a.pid < b.pid;
              });
    return result;
}

} // namespace terminjector::ProcessHelper
