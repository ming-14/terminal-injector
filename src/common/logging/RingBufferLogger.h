// RingBufferLogger：无锁日志器（thread_local ring buffer + 后台刷盘）
// 详见 docs/phases/10-state-sync-stability.md 4.6
//
// Phase 10 任务4.6：替换 Phase 1 的 SRWLOCK 全局队列实现
//
// 设计要点：
//   - 每个 Hook 线程独立 thread_local ring buffer（SPSC 无锁）
//   - 入队路径无锁：仅访问 thread_local buffer，无 SRWLOCK 竞争
//   - 后台刷盘线程 10ms 扫描所有 buffer，批量 WriteFile
//   - 线程通过 thread_local shared_ptr 持有 buffer（线程退出时自动脱离）
//   - ThreadRegistry Compact 清理 use_count==1 的 buffer（线程已退出）
//
// 无锁保证：
//   - 入队：仅 thread_local buffer 操作（SPSC，head/tail 原子变量）
//   - 后台刷盘：Snapshot 所有 buffer（shared_lock），批量读取（SPSC 无锁）
//   - Registry 操作：仅在首次 Log（注册）和后台 Compact 时加锁
//
// 缓冲区满策略：丢弃新日志（不阻塞热路径）
//   - 512KB per thread 足够缓存 10ms 内的日志（>250 条/线程）
//   - 满丢弃比阻塞更符合"日志不阻塞 Hook"原则
//   - 满丢弃比覆盖旧日志更安全（避免后台线程读到半写数据）
//
// ODS 策略：仍每条调用（保持 DebugView 实时查看体验）
//   - ODS 是 kernel32 API，内部线程安全，开销主要在 UTF-8→UTF-16 转换
//   - 文件写入路径无锁化已消除主要瓶颈，ODS 优化待性能数据支持再做
#pragma once

#include "LogLevel.h"
#include <windows.h>
#include <atomic>
#include <memory>
#include <string>
#include <vector>
#include <thread>

namespace terminjector {

// ============================================================
// ThreadLogBuffer：单线程写、单线程读的无锁环形缓冲区
// ============================================================
// 生产者：Hook 线程本身（Log 调用）
// 消费者：后台刷盘线程
//
// SPSC 内存序：
//   - 生产者：load tail (acquire) → 写数据 → store head (release)
//   - 消费者：load head (acquire) → 读数据 → store tail (release)
//   - acquire/release 配对保证数据可见性
class ThreadLogBuffer {
public:
    static constexpr size_t kEntrySize = 2048;   // 单条日志最大字节数（含 \n\0）
    static constexpr size_t kEntryCount = 256;   // 256 条 = 512KB per thread

    ThreadLogBuffer() = default;

    // 写入一条日志（仅线程本身调用）
    // data/len: 已格式化的日志字节（不含 \n\0，函数内部追加）
    // 返回 true 表示成功写入，false 表示缓冲区满（丢弃）
    bool TryPush(const char* data, size_t len);

    // 读取所有可读日志到 out（仅后台线程调用）
    // 返回读取的条数
    size_t ConsumeAll(std::vector<std::string>& out);

private:
    // head/tail 始终递增（不回绕），实际位置 = pos % kEntryCount
    // 这样避免 head/tail 回绕时的复杂判断
    alignas(64) std::atomic<size_t> m_head{0};  // 写位置（生产者写，消费者读）
    alignas(64) std::atomic<size_t> m_tail{0};  // 读位置（消费者写，生产者读）
    alignas(64) char m_data[kEntryCount * kEntrySize];
};

// ============================================================
// ThreadRegistry：线程注册表，管理所有活跃的 ThreadLogBuffer
// ============================================================
// 后台刷盘线程通过 Snapshot 遍历所有 buffer 批量读取
// Compact 清理已退出线程的 buffer（use_count==1 表示仅 Registry 持有）
class ThreadRegistry {
public:
    static ThreadRegistry& Instance();

    // 注册一个 buffer（线程首次调用 Logger 时）
    void Register(std::shared_ptr<ThreadLogBuffer> buf);

    // 获取所有 buffer 的快照（后台线程调用，shared_lock）
    std::vector<std::shared_ptr<ThreadLogBuffer>> Snapshot();

    // 清理已退出线程的 buffer（use_count==1 表示仅 Registry 持有）
    // 返回清理的条数
    size_t Compact();

private:
    ThreadRegistry() = default;
    SRWLOCK m_lock = SRWLOCK_INIT;
    std::vector<std::shared_ptr<ThreadLogBuffer>> m_buffers;
};

// ============================================================
// RingBufferLogger：无锁日志器主类
// ============================================================
class RingBufferLogger {
public:
    // 初始化日志文件 + 启动后台刷盘线程
    void Initialize(const std::wstring& logPath, LogLevel minLevel);

    // 关闭日志（排空所有 buffer 后停止后台线程）
    void Shutdown();

    // 写日志（可变参数版本，内部转 va_list 调 LogV）
    void Log(LogLevel level, const char* fmt, ...);

    // 写日志（va_list 版本，供 Logger 外观转发）
    void LogV(LogLevel level, const char* fmt, va_list args);

    bool IsInitialized() const { return m_initialized.load(std::memory_order_acquire); }

    void* GetFileHandle() const { return reinterpret_cast<void*>(m_fileHandle); }

private:
    // 获取当前线程的 buffer（首次调用时创建并注册到 ThreadRegistry）
    std::shared_ptr<ThreadLogBuffer> GetThreadBuffer();

    // 后台刷盘线程主循环
    void WorkerMain();

    // 批量写入文件（设 t_inLogImpl 防止 WriteFile_Detour 递归）
    void FlushBatch(std::vector<std::string>& batch);

    HANDLE m_fileHandle = INVALID_HANDLE_VALUE;
    std::atomic<LogLevel> m_minLevel{LogLevel::Info};
    std::atomic<bool> m_initialized{false};
    std::atomic<bool> m_shutdown{false};
    std::thread m_worker;

    // 线程局部递归保护标志
    // 仅 worker 线程在 WriteFile 时设置：WriteFile 可能触发 WriteFile_Detour，
    // Detour 内调 Logger 会被 LogV 顶部 t_inLogImpl 检查拦截（丢弃）
    static thread_local bool t_inLogImpl;
};

} // namespace terminjector
