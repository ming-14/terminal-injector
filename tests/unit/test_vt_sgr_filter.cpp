// VtSgrFilter 隔离自测：直接编译本 .cpp，验证 SGR 重建语义与换行归一化。
//
// 重点回归（2026-09-25）：冒号式真彩色 `38:2::r:g:b` 的空保留位。
//   修复前把 ':' 当 ';' 拍平 → 重建为 `38;2;;r;g;` → WR 取缺省 0
//   → 实际收到 (0,r,g)，真彩色渐变整条反相、尾端变黑。
//   修复后必须**逐字节原样**重建。
//
// 重点回归（2026-09-25 第二轮）：裸 LF → CRLF 归一（termtest 空行丢失）。
//   ConHost 把裸 LF 当 CR+LF，WT 只当纯换行 → 直通裸 LF 时新行接在行尾，
//   列号累加导致空行视觉丢失。见 VtSgrFilter.h「换行归一化」。
//
//   cl /EHsc /std:c++17 test_vt_sgr_filter.cpp ../VtSgrFilter.cpp && test_vt_sgr_filter.exe
#include "VtSgrFilter.h"

#include <cstdio>
#include <string>

using terminjector::vt::VtSgrFilter;

static int g_fail = 0;
static int g_pass = 0;

static std::string Run(const std::string& in) {
    std::string out;
    VtSgrFilter::Instance().Reset();
    VtSgrFilter::Instance().Process(in.data(), in.size(), out);
    return out;
}

// 分片喂入：验证归一状态（m_lastWasCr / 状态机）跨 Process() 调用保持。
static std::string RunSplit(const std::string& a, const std::string& b) {
    std::string out;
    VtSgrFilter::Instance().Reset();
    VtSgrFilter::Instance().Process(a.data(), a.size(), out);
    VtSgrFilter::Instance().Process(b.data(), b.size(), out);
    return out;
}

static void Report(const char* name, const std::string& in,
                   const std::string& want, const std::string& got) {
    if (got == want) {
        ++g_pass;
        std::printf("  PASS  %-42s ->\n", name);
        return;
    }
    ++g_fail;
    std::printf("  FAIL  %s\n", name);
    std::printf("        in   =");
    for (unsigned char c : in) std::printf(" %02X", c);
    std::printf("\n        want =");
    for (unsigned char c : want) std::printf(" %02X", c);
    std::printf("\n        got  =");
    for (unsigned char c : got) std::printf(" %02X", c);
    std::printf("\n");
}

static void Expect(const char* name, const std::string& in,
                   const std::string& want) {
    Report(name, in, want, Run(in));
}

int main() {
    std::printf("VtSgrFilter 隔离自测\n");

    // ---- 冒号式真彩色：必须原样（本 bug 的核心） ----
    Expect("colon truecolor fg reserved-empty",
           "\x1b[38:2::255:0:0m", "\x1b[38:2::255:0:0m");
    Expect("colon truecolor bg reserved-empty",
           "\x1b[48:2::255:0:0m", "\x1b[48:2::255:0:0m");
    Expect("colon truecolor fg no-reserved",
           "\x1b[38:2:255:0:0m", "\x1b[38:2:255:0:0m");
    Expect("colon colorspace id",
           "\x1b[38:2:0:255:0:0m", "\x1b[38:2:0:255:0:0m");
    Expect("colon truecolor + reset",
           "\x1b[38:2::255:0:0m" "X" "\x1b[0m",
           "\x1b[38:2::255:0:0m" "X" "\x1b[0m");

    // ---- 分号式真彩色：缺省参数必须保留（R=0 语义） ----
    Expect("semicolon truecolor plain",
           "\x1b[38;2;255;0;0m", "\x1b[38;2;255;0;0m");
    Expect("semicolon truecolor default-channel",
           "\x1b[38;2;;255;0m", "\x1b[38;2;;255;0m");
    Expect("semicolon 256-color",
           "\x1b[38;5;196m", "\x1b[38;5;196m");
    Expect("colon 256-color",
           "\x1b[38:5:196m", "\x1b[38:5:196m");

    // ---- 删除线剥离（过滤器存在的原因，不能被本次修复破坏） ----
    Expect("strip strikethrough only", "\x1b[9m", "");
    Expect("strip 29 only", "\x1b[29m", "");
    Expect("strip 9 keep others", "\x1b[1;9;34m", "\x1b[1;34m");
    Expect("ESC[m reset preserved", "\x1b[m", "\x1b[m");
    Expect("color index 9 not stripped", "\x1b[38;5;9m", "\x1b[38;5;9m");

    // ---- 真彩 + 删除线混合：颜色数据完好、9 被剥离 ----
    // 注意 9 被剥离后，它自己的分隔符 ';' 一并消失 → 尾部不留空参数。
    Expect("colon truecolor then strikethrough",
           "\x1b[38:2::10:20:30;9m", "\x1b[38:2::10:20:30m");

    // ---- 非 SGR / 非 CSI 原样透传 ----
    Expect("plain text", "hello", "hello");
    Expect("cursor move passthrough", "\x1b[2;3H", "\x1b[2;3H");
    Expect("OSC passthrough", "\x1b]0;title\x07", "\x1b]0;title\x07");
    Expect("charset passthrough", "\x1b(B", "\x1b(B");

    // ---- 换行归一化：裸 LF → CRLF（ConHost 换行语义，2026-09-25） ----
    // 裸 LF 必须补 CR，否则 WT 保持列号 → 行右移、空行视觉丢失。
    Expect("bare LF -> CRLF", "AAA\nBBB", "AAA\r\nBBB");
    Expect("two bare LF", "A\n\nB", "A\r\n\r\nB");
    Expect("leading bare LF", "\nX", "\r\nX");
    // 已是 CRLF：不得重复补 CR（否则 \r\r\n，WT 会多空一行）。
    Expect("CRLF unchanged", "AAA\r\nBBB", "AAA\r\nBBB");
    // 裸 CR：原样保留，不补 LF。
    Expect("bare CR unchanged", "AAA\rBBB", "AAA\rBBB");
    // 裸 CR + 裸 LF（分两段写）归一后等价于 CRLF。
    Expect("lone CR then LF", "AAA\r" "\nBBB", "AAA\r\nBBB");
    // CR 连写：第二个 CR 之后仍需为 LF 补 CR 判定重置正确。
    Expect("CR CR LF", "A\r\r\nB", "A\r\r\nB");
    Expect("CR then text then LF", "A\rB\nC", "A\rB\r\nC");

    // ---- 换行归一化不得污染序列载荷 ----
    // GFX/OSC 载荷内的 0x0A 是数据，必须原样（OSC 只由 BEL/ST 终止）。
    Expect("OSC payload LF untouched",
           "\x1b]0;a\nb\x07", "\x1b]0;a\nb\x07");
    Expect("DCS payload LF untouched",
           "\x1bPa\nb\x1b\\", "\x1bPa\nb\x1b\\");
    // SGR/CSI 序列不含裸 LF；序列后紧跟的裸 LF 仍需归一。
    Expect("CSI then bare LF",
           "\x1b[2J" "AA\nB", "\x1b[2J" "AA\r\nB");

    // ---- 归一状态跨分片保持 ----
    {
        const std::string got = RunSplit("AAA\r", "\nBBB");
        Report("split CR|LF across Process", "AAA\r|\nBBB",
               "AAA\r\nBBB", got);
    }
    {
        // 分片恰好切在裸 LF 前：CR 与 LF 分属两次调用，仍只补一次 CR。
        const std::string got = RunSplit("AAA", "\nBBB");
        Report("split text|LF across Process", "AAA|\nBBB",
               "AAA\r\nBBB", got);
    }
    {
        // 分片切在 CR 之后、LF 之前且中间隔着序列：标记失效，LF 需补 CR。
        const std::string got = RunSplit("A\r", "\x1b[0m" "\nB");
        Report("split CR|CSI LF across Process", "A\r|\x1b[0m\nB",
               "A\r\x1b[0m\r\nB", got);
    }
    // CR 后紧跟 OSC（含 BEL 终止）再裸 LF：标记必须已被 OSC 头部打断，
    // 末尾 LF 需补 CR（回归：曾漏重置导致误判为 CRLF）。
    Expect("CR then OSC then LF",
           "A\r\x1b]0;x\x07\nB", "A\r\x1b]0;x\x07\r\nB");
    // CR 后紧跟单字符 ESC 序列再裸 LF：同理。
    Expect("CR then ESC-M then LF",
           "A\r\x1bM\nB", "A\r\x1bM\r\nB");

    // ---- 跨片分片：状态机保持 ----
    Report("split colon truecolor", "\x1b[38:2::255:|0:0m",
           "\x1b[38:2::255:0:0m",
           RunSplit("\x1b[38:2::255:", "0:0m"));

    std::printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
