// VtSgrFilter 隔离自测：直接编译本 .cpp，验证 SGR 重建语义。
//
// 重点回归（2026-09-25）：冒号式真彩色 `38:2::r:g:b` 的空保留位。
//   修复前把 ':' 当 ';' 拍平 → 重建为 `38;2;;r;g;` → WR 取缺省 0
//   → 实际收到 (0,r,g)，真彩色渐变整条反相、尾端变黑。
//   修复后必须**逐字节原样**重建。
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

static void Expect(const char* name, const std::string& in,
                   const std::string& want) {
    const std::string got = Run(in);
    if (got == want) {
        ++g_pass;
        std::printf("  PASS  %-42s %s\n", name, "->");
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

    // ---- 跨片分片：状态机保持 ----
    {
        const std::string part1 = "\x1b[38:2::255:";   // 13 字节
        const std::string part2 = "0:0m";              // 4 字节
        std::string out;
        VtSgrFilter::Instance().Reset();
        VtSgrFilter::Instance().Process(part1.data(), part1.size(), out);
        VtSgrFilter::Instance().Process(part2.data(), part2.size(), out);
        const std::string want = "\x1b[38:2::255:0:0m";
        if (out == want) {
            ++g_pass;
            std::printf("  PASS  %-42s ->\n", "split colon truecolor");
        } else {
            ++g_fail;
            std::printf("  FAIL  split colon truecolor  got=");
            for (unsigned char c : out) std::printf(" %02X", c);
            std::printf("\n");
        }
    }

    std::printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return g_fail == 0 ? 0 : 1;
}
