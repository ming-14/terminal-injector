// RingBufferLogger 实现：无锁日志器
// 详见 docs/phases/10-state-sync-stability.md 4.6
//
// 实现要点：
//   - ThreadLogBuffer: SPSC 无锁 ring buffer，固定大小，满丢弃
//   - ThreadRegistry: SRWLOCK 保护，shared_ptr 持有 buffer
//   - RingBufferLogger: thread_local buffer + 后台 10ms 刷盘
//
// 线程安全：
//   - LogV 调用：仅访问 thread_local buffer（SPSC 无锁）+ ODS（kernel32 线程安全）
//   - 后台刷盘：Snapshot (shared_lock) + ConsumeAll (SPSC 无锁) + WriteFile (t_inLogImpl 保护)
//   - Registry Compact：exclusive_lock，移除 use_count==1 的 buffer
//
// 生命周期：
//   - Initialize 启动 worker 线程
//   - Shutdown 排空所有 buffer 后停止 worker（用 native handle + 超时等待，
//     兼容 DLL_PROCESS_DETACH 的 Loader Lock 约束）
//   - thread_local shared_ptr 在线程退出时析构，Registry 仍持有引用，
//     Compact 后续清理 use_count==1 的 buffer

#include "RingBufferLogger.h"

#include <cstdio>
#include <cstdarg>
#include <cstring>
#include <algorithm>

namespace terminjector {

// ============================================================
// 线程局部状态
// ============================================================

// 递归保护标志定义（静态成员声明在 .h，定义在 .cpp）
thread_local bool RingBufferLogger::t_inLogImpl = false;

// 线程局部 buffer（首次调用时创建并注册）
// shared_ptr 保证线程退出时 buffer 不被释放（ThreadRegistry 仍持有）
// 后台 Compact 清理 use_count==1 的 buffer
static thread_local std::shared_ptr<ThreadLogBuffer> t_buffer;

// ============================================================
// ThreadLogBuffer 实现
// ============================================================

bool ThreadLogBuffer::TryPush(const char* data, size_t len) {
    size_t head = m_head.load(std::memory_order_relaxed);
    size_t tail = m_tail.load(std::memory_order_acquire);

    // 满判断：head - tail >= kEntryCount
    // size_t 无符号减法，head >= tail 始终成立（SPSC 约定 head >= tail）
    if (head - tail >= kEntryCount) {
        return false;  // 缓冲区满，丢弃新日志（不阻塞热路径）
    }

    // 写入数据到 head 位置
    size_t pos = head % kEntryCount;
    // 留 2 字节给 \n\0，最多复制 kEntrySize - 2 字节
    size_t copyLen = (len < kEntrySize - 2) ? len : kEntrySize - 2;
    std::memcpy(&m_data[pos * kEntrySize], data, copyLen);
    m_data[pos * kEntrySize + copyLen] = '\n';
    m_data[pos * kEntrySize + copyLen + 1] = '\0';

    // 发布：head store release 让消费者看到数据写入
    m_head.store(head + 1, std::memory_order_release);
    return true;
}

size_t ThreadLogBuffer::ConsumeAll(std::vector<std::string>& out) {
    size_t head = m_head.load(std::memory_order_acquire);
    size_t tail = m_tail.load(std::memory_order_relaxed);

    size_t count = head - tail;
    if (count == 0) return 0;

    out.reserve(out.size() + count);
    for (size_t i = 0; i < count; ++i) {
        size_t pos = (tail + i) % kEntryCount;
        // 从 \0 结尾的字符串构造 std::string
        out.emplace_back(&m_data[pos * kEntrySize]);
    }

    // 更新 tail：release 让生产者看到消费完成（腾出空间）
    m_tail.store(head, std::memory_order_release);
    return count;
}

// ============================================================
// ThreadRegistry 实现
// ============================================================

ThreadRegistry& ThreadRegistry::Instance() {
    // Meyers's Singleton：局部静态变量，C++11 起线程安全初始化
    static ThreadRegistry inst;
    return inst;
}

void ThreadRegistry::Register(std::shared_ptr<ThreadLogBuffer> buf) {
    AcquireSRWLockExclusive(&m_lock);
    m_buffers.push_back(std::move(buf));
    ReleaseSRWLockExclusive(&m_lock);
}

std::vector<std::shared_ptr<ThreadLogBuffer>> ThreadRegistry::Snapshot() {
    // shared_lock：允许多个读者并发（后台线程独占读，注册/Compact 独占写）
    AcquireSRWLockShared(&m_lock);
    auto copy = m_buffers;  // 拷贝 shared_ptr 引用（引用计数 +1）
    ReleaseSRWLockShared(&m_lock);
    return copy;
}

size_t ThreadRegistry::Compact() {
    AcquireSRWLockExclusive(&m_lock);
    size_t before = m_buffers.size();
    // use_count()==1 表示仅 Registry 持有，线程已退出（thread_local shared_ptr 已析构）
    m_buffers.erase(
        std::remove_if(m_buffers.begin(), m_buffers.end(),
            [](const std::shared_ptr<ThreadLogBuffer>& buf) {
                return buf.use_count() == 1;
            }),
        m_buffers.end());
    size_t after = m_buffers.size();
    ReleaseSRWLockExclusive(&m_lock);
    return before - after;
}

// ============================================================
// RingBufferLogger 实现
// ============================================================

std::shared_ptr<ThreadLogBuffer> RingBufferLogger::GetThreadBuffer() {
    // 首次调用：创建 buffer 并注册到 ThreadRegistry
    // 后续调用：直接返回 thread_local 缓存的 shared_ptr
    if (!t_buffer) {
        t_buffer = std::make_shared<ThreadLogBuffer>();
        ThreadRegistry::Instance().Register(t_buffer);
    }
    return t_buffer;
}

void RingBufferLogger::Initialize(const std::wstring& logPath, LogLevel minLevel) {
    // 若已有 worker 运行，先停止
    if (m_initialized.load()) {
        Shutdown();
    }

    m_minLevel.store(minLevel, std::memory_order_release);
    m_shutdown.store(false, std::memory_order_release);

    if (m_fileHandle != INVALID_HANDLE_VALUE) {
        CloseHandle(m_fileHandle);
        m_fileHandle = INVALID_HANDLE_VALUE;
    }

    if (!logPath.empty()) {
        // 创建日志文件（每次启动覆盖）
        m_fileHandle = CreateFileW(
            logPath.c_str(),
            GENERIC_WRITE,
            FILE_SHARE_READ,
            nullptr,
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            nullptr);
        if (m_fileHandle == INVALID_HANDLE_VALUE) {
            // 文件打开失败，仅 ODS 兜底
            OutputDebugStringW(L"[terminjector] RingBufferLogger: CreateFileW failed, file logging disabled");
        }
    }

    m_initialized.store(true, std::memory_order_release);

    // 启动后台刷盘线程
    m_worker = std::thread(&RingBufferLogger::WorkerMain, this);
}

void RingBufferLogger::Shutdown() {
    if (!m_initialized.load()) return;

    m_initialized.store(false, std::memory_order_release);
    m_shutdown.store(true, std::memory_order_release);

    // 用 native handle + 超时等待 worker 退出
    // WaitForSingleObject 是 kernel32 API，不依赖 Loader Lock
    if (m_worker.joinable()) {
        HANDLE h = m_worker.native_handle();
        DWORD wait = WaitForSingleObject(h, 2000);
        if (wait == WAIT_OBJECT_0) {
            // worker 已退出，安全 join 回收 std::thread 资源
            m_worker.join();
        } else {
            // 超时（可能 DLL_PROCESS_DETACH 中 Loader Lock 阻止 worker 退出）
            // detach 让 worker 自行退出，不关闭句柄（OS 进程退出时清理）
            m_worker.detach();
        }
    }

    // 排空剩余 buffer（worker 退出后可能还有未刷盘的日志）
    auto buffers = ThreadRegistry::Instance().Snapshot();
    std::vector<std::string> batch;
    for (auto& buf : buffers) {
        buf->ConsumeAll(batch);
    }
    if (!batch.empty()) {
        FlushBatch(batch);
    }

    // 关闭文件句柄（worker 已退出，安全关闭）
    if (m_fileHandle != INVALID_HANDLE_VALUE) {
        FlushFileBuffers(m_fileHandle);
        CloseHandle(m_fileHandle);
        m_fileHandle = INVALID_HANDLE_VALUE;
    }

    // 清理已退出线程的 buffer
    ThreadRegistry::Instance().Compact();
}

void RingBufferLogger::Log(LogLevel level, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    LogV(level, fmt, args);
    va_end(args);
}

void RingBufferLogger::LogV(LogLevel level, const char* fmt, va_list args) {
    // 递归保护：仅 worker 线程 WriteFile 时 t_inLogImpl=true
    // 调用方线程 t_inLogImpl=false，不会丢日志
    if (t_inLogImpl) return;

    if (static_cast<int>(level) < static_cast<int>(
            m_minLevel.load(std::memory_order_acquire))) return;
    if (!m_initialized.load(std::memory_order_acquire) && level < LogLevel::Warn) {
        // 未初始化时只输出 Warn 及以上
        return;
    }

    // 格式化到栈缓冲
    // 注意：snprintf/vsnprintf 返回值是"应写入的字符数"（不含 \0），
    //       当输出被截断时返回值 ≥ 传入 size，但 buf 中实际只写入 size-1 字节。
    //       必须用 clamp 后的值作为索引，否则 buf[totalLen++]='\n' 会越界写入栈
    //       （2026-07-25 修复循环 2 cmd 崩溃：STATUS_STACK_BUFFER_OVERRUN 0xC0000409）
    char buf[ThreadLogBuffer::kEntrySize];
    const int bufSize = static_cast<int>(sizeof(buf));
    int prefixLen = std::snprintf(buf, sizeof(buf), "[%s] ", ToString(level));
    if (prefixLen < 0) prefixLen = 0;
    if (prefixLen > bufSize - 2) prefixLen = bufSize - 2;  // 兜底：至少留 \n\0

    const int bodyMax = bufSize - prefixLen - 2;  // body 可用空间（留 \n\0）
    int bodyLen = std::vsnprintf(buf + prefixLen,
                                 static_cast<size_t>(bodyMax),
                                 fmt, args);
    if (bodyLen < 0) {
        bodyLen = 0;
    } else if (bodyLen > bodyMax) {
        // 输出被截断：vsnprintf 实际只写入 bodyMax-1 字节（末尾 \0）
        // clamp 到 bodyMax-1，确保后续 buf[prefixLen + bodyMax - 1] 不越界
        bodyLen = bodyMax - 1;
    }
    int totalLen = prefixLen + bodyLen;
    buf[totalLen++] = '\n';
    buf[totalLen] = '\0';

    // 路 1：OutputDebugStringW（UTF-8 → UTF-16，实时查看 DebugView）
    // 不依赖任何锁，ODS 内部线程安全
    wchar_t wbuf[4096];
    int wlen = MultiByteToWideChar(CP_UTF8, 0, buf, totalLen, wbuf, 4096);
    if (wlen > 0) {
        OutputDebugStringW(wbuf);
    }

    // 路 2：入队 thread_local ring buffer（无锁，SPSC）
    // 仅访问 thread_local buffer，无 SRWLOCK 竞争
    auto buffer = GetThreadBuffer();
    if (buffer) {
        buffer->TryPush(buf, static_cast<size_t>(totalLen));
    }
}

void RingBufferLogger::WorkerMain() {
    // 后台刷盘循环：10ms 扫描所有 buffer，批量写入文件
    // 不用 ConditionVariable：固定 10ms 轮询足够低延迟，且实现简单
    while (true) {
        Sleep(10);  // 10ms 刷盘间隔（~100fps，足够实时）

        // 收集所有 buffer 的日志
        auto buffers = ThreadRegistry::Instance().Snapshot();
        std::vector<std::string> batch;
        for (auto& buf : buffers) {
            buf->ConsumeAll(batch);
        }

        if (!batch.empty()) {
            FlushBatch(batch);
        }

        // 定期清理已退出线程的 buffer（每 100 次循环 = 1s 清理一次）
        // 避免频繁 Compact 影响 Snapshot 性能
        static size_t compactCounter = 0;
        if (++compactCounter >= 100) {
            compactCounter = 0;
            ThreadRegistry::Instance().Compact();
        }

        // 收到 shutdown 信号：再排空一次后退出
        if (m_shutdown.load(std::memory_order_acquire)) {
            buffers = ThreadRegistry::Instance().Snapshot();
            for (auto& buf : buffers) {
                buf->ConsumeAll(batch);
            }
            if (!batch.empty()) {
                FlushBatch(batch);
            }
            break;
        }
    }
}

void RingBufferLogger::FlushBatch(std::vector<std::string>& batch) {
    // 设 t_inLogImpl=true：WriteFile 可能触发 WriteFile_Detour，
    // Detour 内调 Logger 会被 LogV 顶部 t_inLogImpl 检查拦截（丢弃）
    // Phase 9 HandleRegistry 会注册日志句柄为 protected，届时 Detour 不再拦截
    t_inLogImpl = true;
    HANDLE h = m_fileHandle;  // 原子读取句柄（Initialize 后不变）
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
    batch.clear();
}

} // namespace terminjector
