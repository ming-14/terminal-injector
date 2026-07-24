// Logger 实现：异步日志（调用方入队，后台线程写文件）
// 详见 docs/phases/01-scaffold.md 4.3.3
//
// 异步设计：
//   - 调用方线程：格式化 → OutputDebugString → 入队（SRWLOCK 短暂加锁）
//   - 后台 worker 线程：出队批量 → WriteFile（无每条 flush）
//   - 调用方不做磁盘 I/O，高频 Hook 路径无阻塞
//
// 安全约束：
//   - WriteFile 写日志文件句柄（FILE_TYPE_DISK），不是 Console 句柄
//   - Phase 9 Hook WriteFile 时需排除此句柄（HandleRegistry 注册为 protected）
//   - worker 线程 WriteFile 时设 t_inLogImpl=true，防止 Detour 递归调用 Logger
//   - 调用方入队路径不调 WriteFile，t_inLogImpl 无需设（调用方日志不丢失）
//   - SRWLOCK + CONDITION_VARIABLE 均为 kernel32 原生，不依赖 CRT 锁
//
// 生命周期：
//   - Initialize 启动 worker 线程
//   - Shutdown 信号 worker 排空队列后退出（用 native handle + 超时等待，
//     兼容 DLL_PROCESS_DETACH 的 Loader Lock 约束）
#include "Logger.h"

#include <windows.h>
#include <cstdio>
#include <cstdarg>
#include <cstring>
#include <string>
#include <deque>
#include <thread>
#include <atomic>
#include <vector>

namespace terminjector {

// ============================================================
// 全局状态
// ============================================================
// 队列保护：SRWLOCK + CONDITION_VARIABLE（kernel32 原生，无 CRT 依赖）
static SRWLOCK                g_queueLock  = SRWLOCK_INIT;
static CONDITION_VARIABLE     g_cv         = CONDITION_VARIABLE_INIT;
static std::deque<std::string> g_queue;     // 待写入的格式化日志条目

// 文件句柄：Initialize 后不变，Shutdown 后 INVALID（worker 线程原子读取）
static HANDLE                 g_fileHandle = INVALID_HANDLE_VALUE;

// 日志级别：atomic，调用方无锁读取
static std::atomic<LogLevel>  g_minLevel   { LogLevel::Info };

// 初始化/关闭标志
static std::atomic<bool>      g_initialized{ false };
static std::atomic<bool>      g_shutdown   { false };

// 后台写线程
static std::thread            g_worker;

// 线程局部递归保护标志
// 仅 worker 线程在 WriteFile 时设置：
//   WriteFile 可能触发 WriteFile_Detour，Detour 内调 Logger 会入队，
//   但 worker 的 t_inLogImpl=true 时 LogImpl 直接返回（丢弃，避免无限递归）
// 调用方线程不设置：入队路径不调 WriteFile，无递归风险，日志不丢失
static thread_local bool t_inLogImpl = false;

// ============================================================
// 后台写线程：出队 → 批量 WriteFile
// ============================================================
static void WorkerMain() {
    while (true) {
        std::vector<std::string> batch;

        // 等待队列有数据或收到 shutdown 信号
        AcquireSRWLockExclusive(&g_queueLock);
        while (g_queue.empty() && !g_shutdown.load(std::memory_order_acquire)) {
            SleepConditionVariableSRW(&g_cv, &g_queueLock, INFINITE, 0);
        }
        // 取出所有待写入条目（move 语义，避免拷贝）
        batch.reserve(g_queue.size());
        for (auto& s : g_queue) {
            batch.push_back(std::move(s));
        }
        g_queue.clear();
        ReleaseSRWLockExclusive(&g_queueLock);

        // 批量写入文件
        // 设 t_inLogImpl=true：WriteFile 可能触发 WriteFile_Detour，
        // Detour 内调 Logger 会被 LogImpl 顶部 t_inLogImpl 检查拦截（丢弃）
        // Phase 9 HandleRegistry 会注册日志句柄为 protected，届时 Detour 不再拦截
        t_inLogImpl = true;
        HANDLE h = g_fileHandle;  // 原子读取句柄（Initialize 后不变）
        if (h != INVALID_HANDLE_VALUE) {
            DWORD written = 0;
            for (const auto& s : batch) {
                BOOL ok = WriteFile(h, s.data(), static_cast<DWORD>(s.size()),
                          &written, nullptr);
                if (!ok) {
                    // 写入失败，ODS 报告
                    wchar_t wbuf[128];
                    int wl = MultiByteToWideChar(CP_UTF8, 0, s.data(), -1, wbuf, 128);
                    if (wl > 0) OutputDebugStringW(wbuf);
                    break;
                }
            }
            FlushFileBuffers(h);
        }
        t_inLogImpl = false;

        // 收到 shutdown 信号且队列已排空：退出
        if (g_shutdown.load(std::memory_order_acquire)) {
            break;
        }
    }
}

// ============================================================
// 初始化日志文件 + 启动 worker 线程
// ============================================================
void Logger::Initialize(const std::wstring& logPath, LogLevel minLevel) {
    // 若已有 worker 运行，先停止
    if (g_initialized.load()) {
        Shutdown();
    }

    g_minLevel.store(minLevel, std::memory_order_release);
    g_shutdown.store(false, std::memory_order_release);

    AcquireSRWLockExclusive(&g_queueLock);
    if (g_fileHandle != INVALID_HANDLE_VALUE) {
        CloseHandle(g_fileHandle);
        g_fileHandle = INVALID_HANDLE_VALUE;
    }

    if (!logPath.empty()) {
        // 确保父目录存在（简易处理：不递归创建，调用方需保证目录存在）
        g_fileHandle = CreateFileW(
            logPath.c_str(),
            GENERIC_WRITE,
            FILE_SHARE_READ,
            nullptr,
            CREATE_ALWAYS,        // 每次启动覆盖
            FILE_ATTRIBUTE_NORMAL,
            nullptr);
        if (g_fileHandle == INVALID_HANDLE_VALUE) {
            // 文件打开失败，仅 OutputDebugString 兜底
            OutputDebugStringW(L"[terminjector] Logger: CreateFileW failed, file logging disabled");
        }
    }
    g_queue.clear();
    ReleaseSRWLockExclusive(&g_queueLock);

    g_initialized.store(true, std::memory_order_release);

    // 启动后台写线程
    g_worker = std::thread(WorkerMain);
}

// ============================================================
// 关闭日志：信号 worker 排空队列后退出
// ============================================================
// 用 native handle + WaitForSingleObject 等待 worker 退出：
//   - app 侧（main.cpp）：worker 通常 2s 内排空队列并退出，安全 join + 关闭句柄
//   - DLL 侧（DLL_PROCESS_DETACH）：若 Loader Lock 阻止线程退出，超时后
//     detach 放弃等待，不关闭句柄（OS 进程退出时清理），避免卡死
void Logger::Shutdown() {
    if (!g_initialized.load()) return;

    g_initialized.store(false, std::memory_order_release);
    g_shutdown.store(true, std::memory_order_release);

    // 唤醒 worker 处理剩余条目
    WakeConditionVariable(&g_cv);

    if (g_worker.joinable()) {
        // 用 native handle + 超时等待 worker 退出
        // WaitForSingleObject 是 kernel32 API，不依赖 Loader Lock
        HANDLE h = g_worker.native_handle();
        DWORD wait = WaitForSingleObject(h, 2000);
        if (wait == WAIT_OBJECT_0) {
            // worker 已退出，安全 join 回收 std::thread 资源
            g_worker.join();

            // worker 已退出，安全关闭文件句柄
            AcquireSRWLockExclusive(&g_queueLock);
            if (g_fileHandle != INVALID_HANDLE_VALUE) {
                FlushFileBuffers(g_fileHandle);
                CloseHandle(g_fileHandle);
                g_fileHandle = INVALID_HANDLE_VALUE;
            }
            ReleaseSRWLockExclusive(&g_queueLock);
        } else {
            // 超时（可能 DLL_PROCESS_DETACH 中 Loader Lock 阻止 worker 退出）
            // detach 让 worker 自行退出，不关闭句柄（OS 进程退出时清理）
            g_worker.detach();
        }
    }
}

bool Logger::IsInitialized() {
    return g_initialized.load(std::memory_order_acquire);
}

void* Logger::GetFileHandle() {
    return reinterpret_cast<void*>(g_fileHandle);
}

// ============================================================
// 核心写日志函数（调用方线程执行）
// ============================================================
// 调用方路径：格式化 → ODS → 入队（无磁盘 I/O，无 FlushFileBuffers）
static void LogImpl(LogLevel level, const char* fmt, va_list args) {
    // 递归保护：仅 worker 线程 WriteFile 时 t_inLogImpl=true
    // 调用方线程 t_inLogImpl=false，不会丢日志
    if (t_inLogImpl) return;

    if (static_cast<int>(level) < static_cast<int>(
            g_minLevel.load(std::memory_order_acquire))) return;
    if (!g_initialized.load(std::memory_order_acquire) && level < LogLevel::Warn) {
        // 未初始化时只输出 Warn 及以上
        return;
    }

    // 格式化到栈缓冲
    char buf[2048];
    int prefixLen = std::snprintf(buf, sizeof(buf), "[%s] ", ToString(level));
    if (prefixLen < 0) prefixLen = 0;

    int bodyLen = std::vsnprintf(buf + prefixLen,
                                 sizeof(buf) - prefixLen - 2,  // 留 \n\0
                                 fmt, args);
    if (bodyLen < 0) bodyLen = 0;
    int totalLen = prefixLen + bodyLen;
    buf[totalLen++] = '\n';
    buf[totalLen]   = '\0';

    // 路 1：OutputDebugStringW（UTF-8 → UTF-16，实时查看 DebugView）
    // 不依赖 g_queueLock，ODS 内部线程安全
    wchar_t wbuf[4096];
    int wlen = MultiByteToWideChar(CP_UTF8, 0, buf, totalLen, wbuf, 4096);
    if (wlen > 0) {
        OutputDebugStringW(wbuf);
    }

    // 路 2：入队等 worker 写文件（SRWLOCK 短暂加锁，无磁盘 I/O）
    AcquireSRWLockExclusive(&g_queueLock);
    g_queue.emplace_back(buf, static_cast<size_t>(totalLen));
    ReleaseSRWLockExclusive(&g_queueLock);

    // 唤醒 worker 写入
    WakeConditionVariable(&g_cv);
}

void Logger::Log(LogLevel level, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    LogImpl(level, fmt, args);
    va_end(args);
}

void Logger::Trace(const char* fmt, ...) {
    va_list args; va_start(args, fmt); LogImpl(LogLevel::Trace, fmt, args); va_end(args);
}
void Logger::Debug(const char* fmt, ...) {
    va_list args; va_start(args, fmt); LogImpl(LogLevel::Debug, fmt, args); va_end(args);
}
void Logger::Info(const char* fmt, ...) {
    va_list args; va_start(args, fmt); LogImpl(LogLevel::Info, fmt, args); va_end(args);
}
void Logger::Warn(const char* fmt, ...) {
    va_list args; va_start(args, fmt); LogImpl(LogLevel::Warn, fmt, args); va_end(args);
}
void Logger::Error(const char* fmt, ...) {
    va_list args; va_start(args, fmt); LogImpl(LogLevel::Error, fmt, args); va_end(args);
}
void Logger::Fatal(const char* fmt, ...) {
    va_list args; va_start(args, fmt); LogImpl(LogLevel::Fatal, fmt, args); va_end(args);
}

} // namespace terminjector
