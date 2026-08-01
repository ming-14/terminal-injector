// VtParser 实现：轻量 VT 解析器
// 详见 docs/phases/14-virtual-console-state.md 4.5
//
// 解析逻辑：
//   1. 累积字节到 m_pending
//   2. 查找 \x1b[（CSI 引入符）
//   3. 判断 CSI 后是否为 '?'（DA 响应）或数字（DSR CPR）
//   4. DSR CPR: 查找 'R' 结束符，提取 row;col 数字
//   5. DA:      查找 'c' 结束符，提取终端能力标识
//   6. 对应回调并移除已匹配部分
#include "VtParser.h"
#include "logging/Logger.h"

#include <cstdlib>

namespace terminjector {

void VtParser::Feed(const uint8_t* data, size_t len) {
    if (data == nullptr || len == 0) return;

    m_pending.append(reinterpret_cast<const char*>(data), len);
    TryParse();
}

void VtParser::TryParse() {
    while (!m_pending.empty()) {
        // 查找 CSI 引入符 \x1b[
        auto csiPos = m_pending.find("\x1b[");
        if (csiPos == std::string::npos) {
            // 没有 CSI 序列，丢弃所有数据（不是 VT 响应）
            m_pending.clear();
            return;
        }

        // 跳过 CSI 之前的字节（不匹配的字节）
        if (csiPos > 0) {
            m_pending.erase(0, csiPos);
            // 现在 m_pending 以 \x1b[ 开头
        }

        // 检查 m_pending 长度是否足够判断类型
        if (m_pending.size() < 3) {
            // 需要至少 \x1b[? 或 \x1b[N 才能判断类型
            return;
        }

        // 判断 CSI 后是否为 '?'（DA 响应以 \x1b[? 开头）
        bool isDaResponse = (m_pending[2] == '?');

        if (isDaResponse) {
            // Phase 15：DA 响应解析
            // 格式：\x1b[?1;Psc 或 \x1b[?c（无参数时）
            // 查找 'c' 结束符（从 \x1b[? 之后开始，位置 3）
            auto cPos = m_pending.find('c', 3);
            if (cPos == std::string::npos) {
                // 未找到完整的 DA 响应，等待更多数据
                return;
            }

            // 提取参数部分（\x1b[? 之后、'c' 之前）
            std::string params = m_pending.substr(3, cPos - 3);
            int caps = 0;
            // 格式：1;Ps（如 "1;61" 表示 VT320）
            if (!params.empty()) {
                auto semicolon = params.find(';');
                if (semicolon != std::string::npos) {
                    // 取分号后的 Ps 部分
                    caps = std::atoi(params.substr(semicolon + 1).c_str());
                }
            }
            if (m_daCb) {
                LOG_INFO("VtParser: DA response detected, caps=%d raw=%s",
                         caps, params.c_str());
                m_daCb(caps);
            } else {
                LOG_DEBUG("VtParser: DA response detected, caps=%d (no callback)",
                         caps);
            }

            // 移除已处理的序列（包括 \x1b[?...c）
            m_pending.erase(0, cPos + 1);
        } else {
            // DSR CPR 响应解析
            // 格式：\x1b[row;colR
            // 在 \x1b[ 之后查找 'R'（从位置 2 开始）
            auto rPos = m_pending.find('R', 2);
            if (rPos == std::string::npos) {
                // 未找到完整的 DSR CPR 响应，等待更多数据
                return;
            }

            // 提取 row;col 部分（\x1b[ 之后、'R' 之前）
            std::string params = m_pending.substr(2, rPos - 2);
            // 格式：row;col（如 "6;1" 表示第 6 行第 1 列）
            auto semicolon = params.find(';');
            if (semicolon != std::string::npos) {
                int row = std::atoi(params.substr(0, semicolon).c_str());
                int col = std::atoi(params.substr(semicolon + 1).c_str());
                if (row > 0 && col > 0 && m_cursorCb) {
                    LOG_INFO("VtParser: DSR CPR detected, row=%d col=%d", row, col);
                    m_cursorCb(col, row);  // col, row 都是 1-based
                } else {
                    LOG_DEBUG("VtParser: DSR CPR params invalid, raw=%s", params.c_str());
                }
            }

            // 移除已处理的序列（包括 \x1b[...R）
            m_pending.erase(0, rPos + 1);
        }
    }
}

} // namespace terminjector