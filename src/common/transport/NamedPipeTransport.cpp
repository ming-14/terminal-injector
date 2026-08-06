// NamedPipeTransport 实现
// 详见 docs/phases/01-scaffold.md 4.4.3
//
// 关键点：
//   - Server 端用 PIPE_WAIT 阻塞模式，配合上层独立线程
//   - Client 端连接时若服务端忙（ERROR_PIPE_BUSY），等待 5 秒重试
//   - Send/Recv 用循环确保完整收发（WriteFile/ReadFile 可能分多次完成）
//   - MakeRandomPipeName 生成 \\.\pipe\terminjector_<pid>_<hex16>
//     （安全加固：随机后缀，防可预测抢占；名字经注入参数传给 DLL）
//   - Create() 用当前用户 SID 的 DACL（防跨用户连接）
#include "NamedPipeTransport.h"
#include "../logging/Logger.h"

#include <cstdio>
#include <cstring>
#include <vector>
#include <sddl.h>  // ConvertSidToStringSidW / ConvertStringSecurityDescriptorToSecurityDescriptorW

namespace terminjector {

// 管道缓冲区大小（64KB，足够鼠标攒批等大包）
namespace {
constexpr DWORD kPipeBufSize = 65536;
}

// 生成 16 位十六进制随机后缀（8 字节强随机）
// 用 RtlGenRandom（advapi32!SystemFunction036，动态获取避免改链接）：
//   rand_s 在部分 CRT 配置下不可用；RtlGenRandom 是系统级 CSPRNG
std::wstring MakeRandomPipeName(uint32_t targetPid) {
    BYTE bytes[8] = {0};
    BOOL ok = FALSE;
    HMODULE hAdvapi = GetModuleHandleW(L"advapi32.dll");
    if (hAdvapi != nullptr) {
        typedef BOOLEAN(WINAPI* RtlGenRandomFn)(PVOID, ULONG);
        auto fn = reinterpret_cast<RtlGenRandomFn>(
            GetProcAddress(hAdvapi, "SystemFunction036"));
        if (fn != nullptr) {
            ok = fn(bytes, sizeof(bytes)) != FALSE;
        }
    }
    if (!ok) {
        // 极罕见回退：时间戳 + pid，保证至少跨会话不同
        const uint64_t fallback =
            (static_cast<uint64_t>(GetTickCount()) << 32) |
            GetCurrentProcessId();
        std::memcpy(bytes, &fallback, sizeof(bytes));
    }
    unsigned int a = (bytes[0] << 24) | (bytes[1] << 16) | (bytes[2] << 8) | bytes[3];
    unsigned int b = (bytes[4] << 24) | (bytes[5] << 16) | (bytes[6] << 8) | bytes[7];
    wchar_t buf[160];
    int n = std::swprintf(buf, sizeof(buf) / sizeof(buf[0]),
                          L"\\\\.\\pipe\\terminjector_%u_%08X%08X",
                          targetPid, a, b);
    return (n > 0) ? std::wstring(buf) : std::wstring();
}

// 构造仅当前用户 + SYSTEM 可访问的 DASD DACL 安全描述符
// 返回 SECURITY_ATTRIBUTES（调用方应在 CreateNamedPipeW 后调用
// ReleaseSecurityAttributes 释放 descriptor）
namespace {

struct SaGuard {
    SECURITY_ATTRIBUTES sa{};
    PSECURITY_DESCRIPTOR sd = nullptr;
    ~SaGuard() {
        if (sd != nullptr) LocalFree(sd);
    }
};

bool BuildCurrentUserSecurityAttributes(SaGuard& out) {
    // 1. 当前进程 token 的 User SID
    HANDLE hToken = nullptr;
    if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &hToken)) {
        LOG_WARN("DACL: OpenProcessToken failed: %lu", GetLastError());
        return false;
    }
    DWORD need = 0;
    GetTokenInformation(hToken, TokenUser, nullptr, 0, &need);
    std::vector<BYTE> buf(need);
    TOKEN_USER* tu = reinterpret_cast<TOKEN_USER*>(buf.data());
    if (!GetTokenInformation(hToken, TokenUser, tu, need, &need)) {
        LOG_WARN("DACL: GetTokenInformation(TokenUser) failed: %lu", GetLastError());
        CloseHandle(hToken);
        return false;
    }
    CloseHandle(hToken);

    // 2. SID -> 字符串（拼 SDDL）
    LPWSTR sidStr = nullptr;
    if (!ConvertSidToStringSidW(tu->User.Sid, &sidStr)) {
        LOG_WARN("DACL: ConvertSidToStringSidW failed: %lu", GetLastError());
        return false;
    }
    std::wstring sddl = L"D:(A;;GA;;;SY)(A;;GA;;;" +
                        std::wstring(sidStr) + L")";
    LocalFree(sidStr);

    // 3. SDDL -> SECURITY_DESCRIPTOR
    if (!ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl.c_str(), SDDL_REVISION_1, &out.sd, nullptr)) {
        LOG_WARN("DACL: ConvertStringSecurityDescriptorToSecurityDescriptor failed: %lu",
                 GetLastError());
        return false;
    }
    out.sa.nLength = sizeof(SECURITY_ATTRIBUTES);
    out.sa.lpSecurityDescriptor = out.sd;
    out.sa.bInheritHandle = FALSE;
    return true;
}

} // namespace

NamedPipeTransport::NamedPipeTransport(std::wstring pipeName, Role role)
    : m_pipeName(std::move(pipeName)), m_role(role) {}

NamedPipeTransport::~NamedPipeTransport() {
    Close();
}

void NamedPipeTransport::Close() {
    if (m_pipeHandle != INVALID_HANDLE_VALUE) {
        // 中介端先 FlushFileBuffers 再 DisconnectNamedPipe 再 CloseHandle
        // 客户端端只 CloseHandle
        FlushFileBuffers(m_pipeHandle);
        if (m_role == Role::Server) {
            DisconnectNamedPipe(m_pipeHandle);
        }
        CloseHandle(m_pipeHandle);
        m_pipeHandle = INVALID_HANDLE_VALUE;
    }
    m_created = false;
}

bool NamedPipeTransport::Create() {
    if (m_role != Role::Server) {
        LOG_ERROR("NamedPipeTransport::Create only valid for Server role");
        return false;
    }
    if (m_pipeHandle != INVALID_HANDLE_VALUE) {
        return true;  // 已创建
    }
    if (m_pipeName.empty()) {
        LOG_ERROR("NamedPipeTransport::Create: empty pipe name");
        return false;
    }

    // 安全属性：DACL 限制到当前用户 + SYSTEM
    // 默认安全属性会让同会话任意进程可连接（管道承载键盘输入与屏幕内容，
    // 见文件头"管道安全"注释），必须显式收紧
    SaGuard saGuard;
    const bool daclOk = BuildCurrentUserSecurityAttributes(saGuard);

    // 中介：创建命名管道实例（不阻塞，等 WaitClient 再等待客户端）
    m_pipeHandle = CreateNamedPipeW(
        m_pipeName.c_str(),
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        1,               // 仅 1 个实例（中介<->DLL 一对一）
        kPipeBufSize,    // 输出缓冲
        kPipeBufSize,    // 输入缓冲
        0,               // 默认超时（仅 WaitNamedPipe 用，此处不生效）
        daclOk ? &saGuard.sa : nullptr);  // DACL 构建失败时回退默认（记 WARN）
    if (m_pipeHandle == INVALID_HANDLE_VALUE) {
        LOG_ERROR("CreateNamedPipeW failed, err=%lu, name=%ws",
                  GetLastError(), m_pipeName.c_str());
        return false;
    }
    if (daclOk) {
        LOG_INFO("NamedPipe server created with user-DACL: %ws", m_pipeName.c_str());
    } else {
        LOG_WARN("NamedPipe server created WITHOUT tightened DACL: %ws",
                 m_pipeName.c_str());
    }
    m_created = true;
    LOG_INFO("NamedPipe server created (not yet waiting): %ws", m_pipeName.c_str());
    return true;
}

bool NamedPipeTransport::WaitClient() {
    if (m_role != Role::Server) {
        LOG_ERROR("NamedPipeTransport::WaitClient only valid for Server role");
        return false;
    }
    if (!m_created || m_pipeHandle == INVALID_HANDLE_VALUE) {
        LOG_ERROR("NamedPipeTransport::WaitClient: Create() not called first");
        return false;
    }

    // 阻塞等待 DLL 连接
    // ERROR_PIPE_CONNECTED 表示客户端在我们调用 ConnectNamedPipe 之前已连上（合法）
    if (!ConnectNamedPipe(m_pipeHandle, nullptr) &&
        GetLastError() != ERROR_PIPE_CONNECTED) {
        LOG_ERROR("ConnectNamedPipe failed, err=%lu", GetLastError());
        CloseHandle(m_pipeHandle);
        m_pipeHandle = INVALID_HANDLE_VALUE;
        m_created = false;
        return false;
    }
    LOG_INFO("NamedPipe server client connected: %ws", m_pipeName.c_str());
    return true;
}

bool NamedPipeTransport::Connect() {
    if (m_pipeHandle != INVALID_HANDLE_VALUE) {
        return true;  // 已连接
    }
    if (m_pipeName.empty()) {
        LOG_ERROR("NamedPipeTransport::Connect: empty pipe name");
        return false;
    }

    if (m_role == Role::Server) {
        // 向后兼容：Create + WaitClient 一次完成
        // 注意：mediator 模式应分别调用 Create() 与 WaitClient()，
        //       中间插入 SpawnInjector，避免竞态
        if (!Create()) return false;
        return WaitClient();
    }

    // Client（DLL）：连接服务端
    // 服务端可能尚未创建管道，循环重试 5 秒
    for (int attempt = 0; attempt < 50; ++attempt) {
        m_pipeHandle = CreateFileW(
            m_pipeName.c_str(),
            GENERIC_READ | GENERIC_WRITE,
            0, nullptr,
            OPEN_EXISTING,
            0, nullptr);

        if (m_pipeHandle != INVALID_HANDLE_VALUE) {
            LOG_INFO("NamedPipe client connected: %ws", m_pipeName.c_str());
            return true;
        }

        DWORD err = GetLastError();
        if (err == ERROR_PIPE_BUSY) {
            WaitNamedPipeW(m_pipeName.c_str(), 100);
            continue;
        }
        // 其他错误（如 ERROR_FILE_NOT_FOUND）也短暂等待后重试
        Sleep(100);
    }

    LOG_ERROR("NamedPipe client connect timeout: %ws err=%lu",
              m_pipeName.c_str(), GetLastError());
    return false;
}

void NamedPipeTransport::Disconnect() {
    Close();
}

bool NamedPipeTransport::IsConnected() const {
    return m_pipeHandle != INVALID_HANDLE_VALUE;
}

uint32_t NamedPipeTransport::GetServerProcessId() const {
    if (m_pipeHandle == INVALID_HANDLE_VALUE) {
        return 0;
    }
    DWORD serverPid = 0;
    if (!GetNamedPipeServerProcessId(m_pipeHandle, &serverPid)) {
        LOG_WARN("GetNamedPipeServerProcessId failed, err=%lu", GetLastError());
        return 0;
    }
    return serverPid;
}

int NamedPipeTransport::Send(const void* data, size_t len) {
    if (m_pipeHandle == INVALID_HANDLE_VALUE || data == nullptr || len == 0) {
        return 0;
    }

    // Phase 5：串行化 Send，避免多线程并发写管道导致数据包交错
    AcquireSRWLockExclusive(&m_sendLock);
    const uint8_t* p = static_cast<const uint8_t*>(data);
    size_t total = 0;
    while (total < len) {
        DWORD toWrite = static_cast<DWORD>(
            (len - total) > 0xFFFFFFFFULL ? 0xFFFFFFFFULL : (len - total));
        DWORD written = 0;
        if (!WriteFile(m_pipeHandle, p + total, toWrite, &written, nullptr)) {
            LOG_ERROR("NamedPipe Send WriteFile failed, err=%lu", GetLastError());
            ReleaseSRWLockExclusive(&m_sendLock);
            return (total > 0) ? static_cast<int>(total) : -1;
        }
        if (written == 0) {
            break;
        }
        total += written;
    }
    ReleaseSRWLockExclusive(&m_sendLock);
    return static_cast<int>(total);
}

int NamedPipeTransport::Recv(void* buf, size_t len) {
    if (m_pipeHandle == INVALID_HANDLE_VALUE || buf == nullptr || len == 0) {
        return 0;
    }

    uint8_t* p = static_cast<uint8_t*>(buf);
    size_t total = 0;
    while (total < len) {
        DWORD toRead = static_cast<DWORD>(
            (len - total) > 0xFFFFFFFFULL ? 0xFFFFFFFFULL : (len - total));
        DWORD read = 0;
        if (!ReadFile(m_pipeHandle, p + total, toRead, &read, nullptr)) {
            DWORD err = GetLastError();
            if (err == ERROR_BROKEN_PIPE) {
                // 对端关闭
                return static_cast<int>(total);
            }
            LOG_ERROR("NamedPipe Recv ReadFile failed, err=%lu", err);
            return (total > 0) ? static_cast<int>(total) : -1;
        }
        if (read == 0) {
            // 对端关闭
            break;
        }
        total += read;
    }
    return static_cast<int>(total);
}

int NamedPipeTransport::Peek(void* buf, size_t len) {
    if (m_pipeHandle == INVALID_HANDLE_VALUE || buf == nullptr || len == 0) {
        return 0;
    }
    DWORD read = 0;
    DWORD avail = 0;
    DWORD leftThisMsg = 0;
    if (!PeekNamedPipe(m_pipeHandle, buf, static_cast<DWORD>(len),
                       &read, &avail, &leftThisMsg)) {
        DWORD err = GetLastError();
        // 管道断开返回 -1（而非 0），让调用方能区分"无数据"(0) 和"管道断开"(-1)
        if (err == ERROR_BROKEN_PIPE) return -1;
        LOG_ERROR("NamedPipe Peek failed, err=%lu", err);
        return -1;
    }
    return static_cast<int>(read);
}

} // namespace terminjector
