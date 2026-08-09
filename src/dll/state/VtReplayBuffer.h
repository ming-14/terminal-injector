// VtReplayBuffer：会话 VT 重放缓冲（Phase 22：卸载时恢复 ConHost 画面）
// 详见 docs/phases/22-conhost-replay.md
//
// 用途：
//   退出注入时把 ConHost 画面恢复为 WT 会话画面。
//   原理：会话期间 ConHost 被冻结（所有写类 API 被 Hook），画面停留在
//   注入时快照；把会话期间发往 WT 的全部 VT 字节记录下来，卸载时重放到
//   ConHost（ConHost 原生支持 VT 处理），最终 ConHost 画面 = 快照 + 增量 = WT 画面。
//
// 设计：
//   - 所有 VtOutput 字节经 BatchSender::EnqueueVtOutput 时追加到本缓冲，
//     覆盖所有发送路径（正常攒批 / LazyInit 前 fallback 直发）
//   - 缓冲保持"从会话开始的连续前缀"：达到上限后停止追加并标记截断
//     （截断时重放出的画面为截断点状态，属资源上限设计）
//   - 线程安全：SRWLOCK 保护追加
//   - 生命周期与 DLL 相同，无需显式清理（DLL 卸载即消失）
#pragma once

#include <windows.h>
#include <atomic>
#include <cstdint>
#include <string>

namespace terminjector {

class VtReplayBuffer {
public:
    static VtReplayBuffer& Instance();

    // 追加会话 VT 字节（线程安全）
    // 返回本次追加的起始偏移（PromptTracker 用作重放截断点）；
    // 返回 -1 表示未入缓冲（空数据 / 超过上限置截断标记，保持连续前缀语义）
    std::int64_t Append(const void* data, size_t len);

    // 缓冲是否达到上限被截断（重放结果缺失尾部内容）
    bool IsTruncated() const;

    // 完整重放数据（追加方保证内容连续）
    const std::string& Data() const;

    // 重放后复位（幂等，供测试/重入使用）
    void Clear();

private:
    VtReplayBuffer() = default;
    ~VtReplayBuffer() = default;
    VtReplayBuffer(const VtReplayBuffer&) = delete;
    VtReplayBuffer& operator=(const VtReplayBuffer&) = delete;

    mutable SRWLOCK m_lock = SRWLOCK_INIT;
    std::string m_data;
    std::atomic<bool> m_truncated{false};
    bool m_warned = false;  // 截断警告只记一次

    // 上限 4MB：约 1000 屏文本，覆盖常见交互会话
    // 超限场景（如 tree /f 海量输出）截断后重放画面为截断点状态
    static constexpr size_t kMaxBytes = 4 * 1024 * 1024;
};

} // namespace terminjector
