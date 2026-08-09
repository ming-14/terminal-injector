// PromptTracker：行编辑 shell 的"读前最后一次输出"追踪
// 详见 docs/phases/22-conhost-replay.md
//
// 用途：
//   卸载重放 ConHost 画面时，把重放终点截断到最后一次 prompt 写入起点，
//   让 shell 被唤醒后自己重绘 prompt（消除双 prompt 累积，替代旧启发式
//   "末行以 > 结尾则擦除"的猜测）。
//
// 原理（写-读序列语义）：
//   行编辑 shell 的循环必然是"画 prompt → 阻塞 ReadConsole"。因此
//   "行编辑读入口之前的最后一次输出写入"就是该读的 prompt —— 不需要
//   猜内容是否以 > 结尾：
//     - 命令输出（如 dir）之后必然跟着下一次 prompt 写入，该写入会覆盖
//       之前记录，最终记录的一定是 prompt
//     - TUI 全屏程序（无 ENABLE_ECHO_INPUT）不记录，永不截断
//
// 线程模型：
//   - 写入侧用 thread_local：prompt 的绘制与后续读发生在同一线程
//     （行编辑 shell 单线程循环），避免其他线程的背景输出污染记录
//   - 读活动计数：卸载侧只有"当前有行编辑读停在阻塞"（shell 正停在
//     prompt 等输入）才截断；命令执行中卸载（长输出）不截断，不丢内容
#pragma once

#include <atomic>
#include <cstdint>
#include <cstddef>
#include <optional>

namespace terminjector {

class PromptTracker {
public:
    static PromptTracker& Instance();

    // 内容写入侧（BatchSender::EnqueueVtOutput 内，recordReplay=true 追加后调用）：
    // replayOffset = 该写入在 VtReplayBuffer 中的起始偏移；-1 = 未入缓冲
    // （空数据 / 缓冲达上限截断，此时 prompt 文本不完整，不记录）
    void OnOutputWrite(std::int64_t replayOffset);

    // 行编辑读进入/离开（ReadConsoleW/A detour 行编辑段，echoEnabled 时生效）
    void OnLineReadBegin();
    void OnLineReadEnd();

    // 卸载侧：存在活动行编辑读且已记录 prompt 候选 → 返回重放截断偏移
    std::optional<std::size_t> TruncateOffset() const;

    // RAII：行编辑读作用域（ctor 快照候选 + 计数++，dtor 计数--）
    class LineReadScope {
    public:
        explicit LineReadScope(bool echoEnabled);
        ~LineReadScope();
        LineReadScope(const LineReadScope&) = delete;
        LineReadScope& operator=(const LineReadScope&) = delete;

    private:
        bool m_active;
    };

private:
    PromptTracker() = default;
    ~PromptTracker() = default;
    PromptTracker(const PromptTracker&) = delete;
    PromptTracker& operator=(const PromptTracker&) = delete;

    // 本线程最近一次内容写入的重放缓冲起始偏移（-1 = 无）
    static thread_local std::int64_t tls_lastWriteOffset;
    // 活动行编辑读计数（>0 = shell 正停在 prompt 等输入）
    std::atomic<int> m_lineReadCount{0};
    // 最近一次行编辑读入口处的写入起点（prompt 候选，-1 = 无）
    std::atomic<std::int64_t> m_lastPromptOffset{-1};
};

} // namespace terminjector
