// VtSgrFilter: 剥离 VT 字节流中 ConHost 无法表达的 SGR 属性（删除线 9/29）
// 详见 docs/phases/04-output-chain.md 与 2026-08-17 vim 欢迎页标题变横线修复
//
// 背景（2026-08-17 vim bug）：
//   劫持环境下 vim 用 xterm 风格 terminfo，清除欢迎页标题时写出
//   ESC[9m + 空格。ConHost 的 16 位属性字没有删除线位，实测完全忽略
//   ESC[9m（写 ESC[1;34;40m ESC[9m 后读回属性字 = 0x0009，与未写 SGR9
//   相同）；而 WT 会渲染删除线空格为横线 → Phase 13 VT 直通把原始字节
//   原样转发，导致 WT 镜像与实际控制台显示分叉（标题变 --------）。
//
//   本过滤器在直通入口按 ConHost 实际渲染模型剥离 SGR 9/29，使发往 WT
//   的流 == ConHost 处理后的流。这不是缓解：ConHost 无删除线语义，镜像
//   必须忠实于目标控制台（与直通路径不翻译/不推进的原则一致）。
//
// 处理规则：
//   - SGR（CSI ... m）：删除顶层参数 9 与 29；若参数全部被删则整条序列
//     丢弃（注意 ESC[m 空参数 = SGR 0 复位，原样保留）。
//   - 38/48/58 颜色引入：其后的模式/索引/通道参数是颜色数据，不剥离
//     （如 ESC[38;5;9m 的 9 是亮红色索引，不是删除线）。
//   - 其余 CSI（光标/清屏/滚屏等）、OSC、DCS/APC/PM、单字符 ESC 序列：
//     原样透传。
//   - 跨 Process() 调用的分片序列：内部状态机保持（与 VtCursorTracker
//     一致），保证 ESC[9 与 m 分两次写入时仍能正确过滤。
#pragma once

#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>

namespace terminjector::vt {

class VtSgrFilter {
public:
    static VtSgrFilter& Instance();

    // 处理一段字节流，过滤后的字节追加到 out（out 可复用，不清空）。
    // 线程安全：内部加锁，保证与 VtCursorTracker::Feed 的调用序一致。
    void Process(const char* data, size_t len, std::string& out);

    // 复位内部状态（会话切换/调试用）。
    void Reset();

private:
    enum class State : uint8_t {
        Ground,       // 普通文本
        EscPending,   // 收到 ESC，待定后续
        CsiCollect,   // 收集 CSI 参数/中间字节，等待最终字节
        Osc,          // OSC 直到 BEL / ST
        Dcs,          // DCS/APC/PM 直到 ST / BEL
        CharsetSel,   // ESC ( ) * + 后接一个字符集字节
    };

    VtSgrFilter() = default;
    ~VtSgrFilter() = default;
    VtSgrFilter(const VtSgrFilter&) = delete;
    VtSgrFilter& operator=(const VtSgrFilter&) = delete;

    void ProcessByte(unsigned char b, std::string& out);
    void FlushCsi(std::string& out);   // 完成一条 CSI
    // 重建 SGR 参数串：剥离 9/29。返回 false 表示整条序列应丢弃。
    bool RebuildSgr(const std::string& params, std::string& out);

    State m_state = State::Ground;
    std::string m_csi;                 // 当前 CSI 原始字节（含 ESC[）
    bool m_oscEsc = false;             // OSC/DCS 内收到 ESC，等待 ST 的 '\\'
    std::mutex m_lock;
};

} // namespace terminjector::vt
