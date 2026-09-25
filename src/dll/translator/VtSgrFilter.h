// VtSgrFilter: 在 VT 直通入口把字节流归一化为「ConHost 处理后的流」
//   1. 剥离 ConHost 无法表达的 SGR 属性（删除线 9/29）
//   2. 裸 LF(0x0A) 补 CR → CRLF（ConHost 的换行语义）
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
//   - 参数分隔符 ':' 与 ';' **分别保留**，重建时按原分隔符拼回。
//     冒号组内的空参数是"颜色空间保留位"（`38:2::r:g:b` 实测等价
//     `38;2;r;g;b`），既不剥离也不占数据位；分号组内的空参数是真实
//     缺省参数（`38;2;;r;g` 的 R=0），占数据位。
//     2026-09-25 修复：此前 ':' 被当 ';' 拍平，`38:2::255:0:0` 重建为
//     `38;2;;255;0;` → WR 取缺省 0 → 收到 (0,255,0)，真彩色渐变整条反相。
//   - 其余 CSI（光标/清屏/滚屏等）、OSC、DCS/APC/PM、单字符 ESC 序列：
//     原样透传。
//   - 跨 Process() 调用的分片序列：内部状态机保持（与 VtCursorTracker
//     一致），保证 ESC[9 与 m 分两次写入时仍能正确过滤。
//   - 换行：Ground 态裸 LF(0x0A) 补前导 CR，见下方「换行归一化」。
//
// 换行归一化（2026-09-25 termtest 256 色段空行丢失修复）：
//   现象：WT 劫持后 termtest 空行消失、后续行右移，与 ConHost 原样输出不一致。
//   根因：ConHost 把裸 LF(0x0A) 当【CR+LF】→ 回到列 0 再下移；WT/wezterm 把裸 LF
//     当【纯换行】→ 只下移、保持列号。程序写 "\n" 时劫持链直通裸 LF，WT 于是把
//     新行接在上一行末尾之后（列号累加），行逐渐右移、触发自动换行多吃一行，
//     视觉上就是空行被"挤掉"。
//   ConHost 回吐实测（tests/_probe/t_conhost_newline_probe.py）：
//       AAA\nBBB    → 回吐 "AAA\r\nBBB"；屏幕 BBB 在列 0
//       原样直通的屏幕    → BBB 在列 3（列号被保留 → 错位）
//       AAA\r\nBBB  → 回吐 "AAA\r\nBBB"（已是 CRLF，不得变成 \r\r\n）
//       AAA\rBBB    → 回吐 "BBB" 于列 0（CR 回行首，不补 LF）
//   → 归一规则（只在 Ground 态生效，GFX/OSC 载荷内的 LF 属数据不得改写）：
//       裸 LF（前一字节非 CR）→ 输出 CR+LF
//       CRLF                 → 原样，不重复补 CR
//       裸 CR                → 原样，不补 LF（与 WT 语义本就一致）
//   归一后 VtCursorTracker 语义不变：追踪器本就把 LF 当 CR+LF
//   （PutCodepoint case 0x0A 已置 X=0），喂入 "\r\n" 与裸 "\n" 状态等价。
//   边界：除裸 LF 外**不再为其他序列做归一**——tests/_probe/t_conhost_csi_norm_probe.py
//   对 \r / EL(0/1/2K) / ED(0/1/2J) / CUP / BS / HT / DECSC-DECRC / 自动换行 / 滚屏
//   逐项对照（基准已过换行归一），10/10 零差异，ConHost 与直通链路语义等价。
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
    // Ground 态字节出口：裸 LF 补 CR（详见头文件「换行归一化」）。
    // 所有非 GS/OSC/DCS 载荷字节都必须经此出口，才能保证归一不漏。
    void EmitGround(unsigned char b, std::string& out);
    // 重建 SGR 参数串：剥离 9/29（分隔符 ';' / ':' 分别保留）。
    // 返回 false 表示整条序列应丢弃。
    bool RebuildSgr(const std::string& params, std::string& out);

    State m_state = State::Ground;
    std::string m_csi;                 // 当前 CSI 原始字节（含 ESC[）
    bool m_oscEsc = false;             // OSC/DCS 内收到 ESC，等待 ST 的 '\\'
    bool m_lastWasCr = false;          // 上一输出字节是 CR（跨 Process 分片保持）
    std::mutex m_lock;
};

} // namespace terminjector::vt
