// VtParser 实现：轻量 VT 解析器
// 详见 docs/phases/14-virtual-console-state.md 4.5
//
// 解析逻辑：
//   1. 累积字节到 m_pending
//   2. 查找 \x1b[（CSI 引入符）
//   3. 在 CSI 后查找 'R'（DSR CPR 结束符）
//   4. 提取 row;col 数字
//   5. 回调并移除已匹配部分
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
            // 没有 CSI 序列，丢弃所有数据（不是 DSR CPR）
            m_pending.clear();
            return;
        }

        // 从 CSI 之后查找 'R'（DSR CPR 结束符）
        // 跳过 CSI 之前的字节（不匹配的字节）
        if (csiPos > 0) {
            m_pending.erase(0, csiPos);
            csiPos = 0;  // 重设，现在 m_pending 以 \x1b[ 开头
        }

        // 在 \x1b[ 之后查找 'R'
        auto rPos = m_pending.find('R', 2);  // 从 \x1b[ 之后开始
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

} // namespace terminjector