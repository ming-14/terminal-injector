// DLL 内部输入事件队列
// 详见 docs/phases/06-input-chain.md 4.2 与 docs/phases/10-state-sync-stability.md 4.2
//
// 设计要点：
//   - 单例（Instance），进程级唯一
//   - 双队列：翻译模式用 m_recordQueue（INPUT_RECORD），
//             透传模式用 m_rawQueue（原始 VT 字节）
//   - SRWLOCK 保护，读写均线程安全
//   - 手动重置事件 m_event：有数据时 signaled，队列空时 reset
//     供 ReadConsoleInput Hook 的 WaitForSingleObject 阻塞等待
//
// 事件管理策略（避免竞态）：
//   - Enqueue: lock → push → SetEvent → unlock
//   - Dequeue: lock → if empty { ResetEvent; unlock; return 0 }
//                    → pop → if now empty { ResetEvent } → unlock → return n
//   - ReadConsoleInput Hook 循环：Dequeue 返回 0 时 WaitForSingleObject 阻塞
//     由于 Dequeue 在 mutex 内 ResetEvent，Enqueue 在 mutex 内 SetEvent,
//     两者互斥，不会丢信号
//
// Phase 10 任务2：鼠标事件攒批
//   - 鼠标高频移动产生大量 MOUSE_EVENT，逐条入队锁开销大
//   - EnqueueBatched 攒批：16ms 或 20 条上限才真正入 m_recordQueue
//   - 键盘事件仍走 EnqueueRecords（即时，低延迟）
//   - 超时 flush：EnqueueBatched 的超时检查依赖下次调用，若无新鼠标事件
//     m_batch 会卡住。DllRecvLoop 在 peek=0 时调 FlushBatchTimeout 补全
//     （最多 16ms 超时 + 10ms 轮询 ≈ 26ms 延迟，< 50ms 要求）
#pragma once

#include <windows.h>
#include <deque>
#include <vector>
#include <chrono>
#include <cstdint>
#include <mutex>

namespace terminjector {

class InputQueue {
public:
    static InputQueue& Instance() {
        // Meyers 单例：局部静态变量，线程安全初始化（C++11 保证）
        static InputQueue inst;
        return inst;
    }

    // ---- 翻译模式：INPUT_RECORD 队列 ----

    // 入队 INPUT_RECORD 数组（DllRecvLoop 收到 VtInput 翻译后调用）
    // 用于键盘事件：即时入队，低延迟
    void EnqueueRecords(const INPUT_RECORD* records, size_t count) {
        if (count == 0) return;
        std::lock_guard<std::mutex> lock(m_mutex);
        for (size_t i = 0; i < count; ++i) {
            m_recordQueue.push_back(records[i]);
        }
        SetEvent(m_event);
    }

    // Phase 10 任务2：鼠标事件攒批入队
    // 攒满 kBatchMaxCount 条 或 kBatchMaxMs 毫秒后才真正入 m_recordQueue
    // 未 flush 时数据留在 m_batch，不 SetEvent，ReadConsoleInput 阻塞等待
    // 超时 flush 由 DllRecvLoop 调 FlushBatchTimeout 触发（解决无新事件时 batch 卡住）
    void EnqueueBatched(const INPUT_RECORD* records, size_t count) {
        if (count == 0) return;
        std::lock_guard<std::mutex> lock(m_mutex);
        auto now = std::chrono::steady_clock::now();
        if (m_batch.empty()) m_batchStart = now;
        for (size_t i = 0; i < count; ++i) m_batch.push_back(records[i]);
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
            now - m_batchStart);
        if (m_batch.size() >= kBatchMaxCount || elapsed.count() >= kBatchMaxMs) {
            FlushBatchLocked();
        }
    }

    // Phase 10 任务2：超时 flush（DllRecvLoop 在 peek=0 空闲时调用）
    // 若 m_batch 非空且超过 kBatchMaxMs，flush 到 m_recordQueue 并 SetEvent
    void FlushBatchTimeout() {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (m_batch.empty()) return;
        auto now = std::chrono::steady_clock::now();
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
            now - m_batchStart);
        if (elapsed.count() >= kBatchMaxMs) {
            FlushBatchLocked();
        }
    }

    // 出队（ReadConsoleInput 用，消费式）
    // 返回实际出队数量；队列为空时返回 0 并 reset 事件
    size_t DequeueRecords(INPUT_RECORD* out, size_t count) {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (m_recordQueue.empty()) {
            ResetEvent(m_event);
            return 0;
        }
        size_t n = 0;
        while (n < count && !m_recordQueue.empty()) {
            out[n++] = m_recordQueue.front();
            m_recordQueue.pop_front();
        }
        if (m_recordQueue.empty() && m_rawQueue.empty()) {
            ResetEvent(m_event);
        }
        return n;
    }

    // 偷窥（PeekConsoleInput 用，不消费）
    size_t PeekRecords(INPUT_RECORD* out, size_t count) const {
        std::lock_guard<std::mutex> lock(m_mutex);
        size_t n = 0;
        for (auto it = m_recordQueue.begin();
             it != m_recordQueue.end() && n < count; ++it, ++n) {
            out[n] = *it;
        }
        return n;
    }

    // ---- 透传模式：原始字节队列 ----

    // 入队原始 VT 字节（ENABLE_VIRTUAL_TERMINAL_INPUT 模式用）
    void EnqueueRaw(const uint8_t* data, size_t len) {
        if (len == 0) return;
        std::lock_guard<std::mutex> lock(m_mutex);
        for (size_t i = 0; i < len; ++i) {
            m_rawQueue.push_back(data[i]);
        }
        SetEvent(m_event);
    }

    // 出队原始字节（ReadFile(CONIN$) 透传用，消费式）
    size_t DequeueRaw(uint8_t* out, size_t len) {
        std::lock_guard<std::mutex> lock(m_mutex);
        if (m_rawQueue.empty()) {
            ResetEvent(m_event);
            return 0;
        }
        size_t n = 0;
        while (n < len && !m_rawQueue.empty()) {
            out[n++] = m_rawQueue.front();
            m_rawQueue.pop_front();
        }
        if (m_recordQueue.empty() && m_rawQueue.empty()) {
            ResetEvent(m_event);
        }
        return n;
    }

    // ---- 通用 ----

    // 当前 INPUT_RECORD 队列长度（GetNumberOfConsoleInputEvents 用）
    // 注意：不含 m_batch 中未 flush 的鼠标事件（攒批期间对程序不可见，最多 16ms）
    size_t RecordCount() const {
        std::lock_guard<std::mutex> lock(m_mutex);
        return m_recordQueue.size();
    }

    // 当前原始字节队列长度
    size_t RawCount() const {
        std::lock_guard<std::mutex> lock(m_mutex);
        return m_rawQueue.size();
    }

    // 清空两个队列（FlushConsoleInputBuffer 用）
    // Phase 10：同时清空 m_batch，避免残留鼠标事件
    void Clear() {
        std::lock_guard<std::mutex> lock(m_mutex);
        m_recordQueue.clear();
        m_rawQueue.clear();
        m_batch.clear();
        ResetEvent(m_event);
    }

    // 入队窗口缓冲区尺寸变化事件（WINDOW_BUFFER_SIZE_EVENT）
    // 当 DLL 收到 ResizeNotify 时调用，通知等待 ReadConsoleInput 的程序
    // （如 Textual）窗口尺寸已变化，让其重新查询 GetConsoleScreenBufferInfo
    void EnqueueResizeEvent(SHORT cols, SHORT rows) {
        INPUT_RECORD rec{};
        rec.EventType = WINDOW_BUFFER_SIZE_EVENT;
        rec.Event.WindowBufferSizeEvent.dwSize.X = cols;
        rec.Event.WindowBufferSizeEvent.dwSize.Y = rows;
        EnqueueRecords(&rec, 1);
    }

    // 模式切换时清空两个队列（避免残留数据混淆）
    void ClearAllOnModeSwitch() {
        Clear();
    }

    // Phase 11：唤醒阻塞在 GetWaitHandle() 上的读取线程
    // Unloader 在 UninstallAll 之前调用，让 ReadConsoleInput Hook 返回（无数据），
    // 之后 Hook 卸载，下次调用走原 API 不再依赖 InputQueue
    void SignalDataReady() {
        SetEvent(m_event);
    }

    // 事件句柄（ReadConsoleInput Hook 用 WaitForSingleObject 等待）
    HANDLE GetWaitHandle() const { return m_event; }

    // 检查是否有任何数据（任一队列非空）
    // 注意：不含 m_batch 未 flush 的鼠标事件
    bool HasData() const {
        std::lock_guard<std::mutex> lock(m_mutex);
        return !m_recordQueue.empty() || !m_rawQueue.empty();
    }

    // 禁止拷贝/移动
    InputQueue(const InputQueue&) = delete;
    InputQueue& operator=(const InputQueue&) = delete;

private:
    InputQueue()
        : m_event(CreateEventW(nullptr, TRUE, FALSE, nullptr))  // 手动重置，初始无信号
    {
    }

    ~InputQueue() {
        if (m_event != nullptr) {
            CloseHandle(m_event);
        }
    }

    // 内部 flush m_batch 到 m_recordQueue（调用方需持锁）
    void FlushBatchLocked() {
        for (const auto& r : m_batch) m_recordQueue.push_back(r);
        m_batch.clear();
        SetEvent(m_event);
    }

    mutable std::mutex       m_mutex;
    std::deque<INPUT_RECORD> m_recordQueue;  // 翻译模式队列
    std::deque<uint8_t>      m_rawQueue;     // 透传模式队列
    HANDLE                   m_event;        // 手动重置事件，有数据时 signaled

    // Phase 10 任务2：鼠标攒批缓冲
    std::vector<INPUT_RECORD>                  m_batch;       // 攒批中的鼠标事件
    std::chrono::steady_clock::time_point      m_batchStart;  // 攒批开始时间
    static constexpr size_t kBatchMaxCount = 20;  // 攒批条数上限
    static constexpr int    kBatchMaxMs    = 16;  // 攒批时间上限（~60fps）
};

} // namespace terminjector
