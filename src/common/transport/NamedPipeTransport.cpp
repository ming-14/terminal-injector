// NamedPipeTransport 实现
// 详见 docs/phases/01-scaffold.md 4.4.3
//
// 关键点：
//   - Server 端用 PIPE_WAIT 阻塞模式，配合上层独立线程
//   - Client 端连接时若服务端忙（ERROR_PIPE_BUSY），等待 5 秒重试
//   - Send/Recv 用循环确保完整收发（WriteFile/ReadFile 可能分多次完成）
//   - MakePipeName 生成 \\.\pipe\terminjector_<pid>
#include "NamedPipeTransport.h"
#include "../logging/Logger.h"

#include <cstdio>

namespace terminjector {

// 管道缓冲区大小（64KB，足够鼠标攒批等大包）
namespace {
constexpr DWORD kPipeBufSize = 65536;
}

// 构造命名管道名称：\\.\pipe\terminjector_<targetPid>
// 中介与 DLL 双方约定一致，DLL 用 GetCurrentProcessId()
std::wstring MakePipeName(uint32_t targetPid) {
    wchar_t buf[128];
    int n = std::swprintf(buf, sizeof(buf) / sizeof(buf[0]),
                          L"\\\\.\\pipe\\terminjector_%u", targetPid);
    return (n > 0) ? std::wstring(buf) : std::wstring();
}

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

    // 中介：创建命名管道实例（不阻塞，等 WaitClient 再等待客户端）
    m_pipeHandle = CreateNamedPipeW(
        m_pipeName.c_str(),
        PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
        1,               // 仅 1 个实例（中介<->DLL 一对一）
        kPipeBufSize,    // 输出缓冲
        kPipeBufSize,    // 输入缓冲
        0,               // 默认超时（仅 WaitNamedPipe 用，此处不生效）
        nullptr);        // 默认安全属性
    if (m_pipeHandle == INVALID_HANDLE_VALUE) {
        LOG_ERROR("CreateNamedPipeW failed, err=%lu, name=%ws",
                  GetLastError(), m_pipeName.c_str());
        return false;
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
