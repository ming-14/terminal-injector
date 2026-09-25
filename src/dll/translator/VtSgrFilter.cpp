// VtSgrFilter 实现：见头文件设计说明
#include "VtSgrFilter.h"

#include <cstdlib>
#include <vector>

namespace terminjector::vt {

VtSgrFilter& VtSgrFilter::Instance() {
    static VtSgrFilter inst;
    return inst;
}

void VtSgrFilter::Process(const char* data, size_t len, std::string& out) {
    if (data == nullptr || len == 0) return;
    std::lock_guard<std::mutex> lock(m_lock);
    for (size_t i = 0; i < len; ++i) {
        ProcessByte(static_cast<unsigned char>(data[i]), out);
    }
}

void VtSgrFilter::Reset() {
    std::lock_guard<std::mutex> lock(m_lock);
    m_state = State::Ground;
    m_csi.clear();
    m_oscEsc = false;
}

void VtSgrFilter::ProcessByte(unsigned char b, std::string& out) {
    switch (m_state) {
        case State::Ground:
            if (b == 0x1B) {
                m_csi.assign(1, static_cast<char>(0x1B));
                m_state = State::EscPending;
            } else {
                out.push_back(static_cast<char>(b));
            }
            break;

        case State::EscPending:
            m_csi.push_back(static_cast<char>(b));
            switch (b) {
                case '[':
                    m_state = State::CsiCollect;
                    break;
                case ']':
                    // OSC/DCS/APC/PM 原样透传：立即输出头部，
                    // 后续内容与 BEL/ST 在对应状态内逐字节输出
                    out += m_csi;
                    m_csi.clear();
                    m_state = State::Osc;
                    m_oscEsc = false;
                    break;
                case 'P':  // DCS
                case '_':  // APC
                case '^':  // PM
                    out += m_csi;
                    m_csi.clear();
                    m_state = State::Dcs;
                    m_oscEsc = false;
                    break;
                case '(':  // 字符集选择，后接一个字节
                case ')':
                case '*':
                case '+':
                    m_state = State::CharsetSel;
                    break;
                default:
                    // 单字符 ESC 序列（7/8/D/M/E/c/H 等）原样透传
                    out += m_csi;
                    m_csi.clear();
                    m_state = State::Ground;
                    break;
            }
            break;

        case State::CsiCollect:
            if (b >= 0x20 && b <= 0x3F) {
                // 参数/中间字节（含 '?' '<' '=' '>' 与 ':' 冒号分隔）
                m_csi.push_back(static_cast<char>(b));
            } else if (b >= 0x40 && b <= 0x7E) {
                // 最终字节：CSI 完成
                m_csi.push_back(static_cast<char>(b));
                FlushCsi(out);
                m_state = State::Ground;
            } else if (b == 0x1B) {
                // 防御：CSI 内出现 ESC（非法），按两段处理
                out += m_csi;
                m_csi.assign(1, static_cast<char>(0x1B));
                m_state = State::EscPending;
            } else {
                // 防御：其余字节（C0/0x80+）中止本 CSI
                out += m_csi;
                m_csi.clear();
                out.push_back(static_cast<char>(b));
                m_state = State::Ground;
            }
            break;

        case State::Osc:
        case State::Dcs:
            // 原样透传（头部已在进入状态时输出）：
            // BEL 或 ST（ESC \）终止，其余字节直接输出
            out.push_back(static_cast<char>(b));
            if (b == 0x07) {
                // BEL 终止
                m_state = State::Ground;
                m_oscEsc = false;
            } else if (b == 0x1B) {
                // 暂存 ESC，等待可能的 ST
                m_oscEsc = true;
            } else if (m_oscEsc) {
                // ST 的 '\' 或 OSC/DCS 内容中的孤立 ESC
                m_oscEsc = false;
                if (b == '\\') {
                    m_state = State::Ground;
                }
            }
            break;

        case State::CharsetSel:
            out += m_csi;      // ESC ( ) * + 与字符集字节一起透传
            m_csi.clear();
            out.push_back(static_cast<char>(b));
            m_state = State::Ground;
            break;
    }
}

void VtSgrFilter::FlushCsi(std::string& out) {
    // m_csi = ESC [ ... final
    const char final = m_csi.back();
    if (final == 'm') {
        // SGR：参数串 = ESC[ 与 m 之间的部分
        const std::string params = m_csi.substr(2, m_csi.size() - 3);
        RebuildSgr(params, out);   // 成功时已把完整序列写入 out；失败=整条丢弃
        return;
    }
    out += m_csi;
}

bool VtSgrFilter::RebuildSgr(const std::string& params, std::string& out) {
    // 拆分参数为 token，并**保留分隔符类型**（';' 还是 ':'）。
    //
    // 为什么必须保留：`38:2::r:g:b` 的冒号组里有一个空（颜色空间保留）位，
    // 实测语义 = `38;2;r;g;b`。若把 ':' 一律当 ';' 拍平并按位置用 ';' 重建，
    // 该序列会变成 `38;2;;r;g;`，WR/WT 解析时 `;;` 取缺省 0 →
    // 实际收到 (R=0, G=r, B=g)，渐变整条反相、尾端被当黑色。
    // （termtest t_sgr.py part_truecolor 的第三条前景/背景渐变即此现象。）
    struct Token {
        std::string text;
        char sep;          // 本 token 之前的分隔符：';' / ':'；'0' 表示首 token
    };
    std::vector<Token> tokens;
    {
        std::string cur;
        char sep = '\0';
        for (char c : params) {
            if (c == ';' || c == ':') {
                tokens.push_back({cur, sep});
                cur.clear();
                sep = c;
            } else {
                cur.push_back(c);
            }
        }
        tokens.push_back({cur, sep});
    }

    // 剥离 9/29，但保护 38/48/58 颜色引入后的模式/索引/通道参数
    std::vector<Token> kept;
    bool expectMode = false;   // 刚输出 38/48/58，下一 token 是颜色模式
    int colorDataLeft = 0;     // 颜色数据（索引/通道）剩余保护数
    for (const Token& t : tokens) {
        if (colorDataLeft > 0) {
            // 冒号组内的空参数是"颜色空间保留位"，不占数据位（实测语义：
            // 它存在与否不影响后续通道解析）→ 跳过且不递减，按原样（含
            // 冒号）保留在输出里。分号组的空参数是真实缺省参数（如
            // `38;2;;r;g` 的 R=0），占数据位、照常保护。
            if (t.text.empty() && t.sep == ':') {
                kept.push_back(t);
                continue;
            }
            kept.push_back(t);
            --colorDataLeft;
            continue;
        }
        if (expectMode) {
            int mode = std::atoi(t.text.c_str());
            colorDataLeft = (mode == 5) ? 1 : (mode == 2) ? 3 : (mode == 4) ? 4 : 1;
            kept.push_back(t);
            expectMode = false;
            continue;
        }
        if (t.text == "38" || t.text == "48" || t.text == "58") {
            kept.push_back(t);
            expectMode = true;
            continue;
        }
        if (t.text == "9" || t.text == "29") {
            continue;  // 删除线，ConHost 无此属性位，剥离
        }
        kept.push_back(t);
    }

    // 空参数（ESC[m）= SGR 0 复位：原样保留
    if (tokens.size() == 1 && tokens[0].text.empty()) {
        out += "\x1b[m";
        return true;
    }
    if (kept.empty()) {
        return false;  // 参数全被剥离（如 ESC[9m），整条丢弃
    }

    // 重建：沿用各 token 自身的分隔符（分隔符承载语义：冒号组里空参数是颜色
    // 空间保留位，分号组里空参数是真实缺省值，重建时必须原样还原）。
    out += "\x1b[";
    for (size_t i = 0; i < kept.size(); ++i) {
        if (i > 0) out.push_back(kept[i].sep == ':' ? ':' : ';');
        out += kept[i].text;
    }
    out.push_back('m');
    return true;
}

} // namespace terminjector::vt
