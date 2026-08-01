// 轻量 VT 解析器（mediator 侧，Phase 14/15）
// 详见 docs/phases/14-virtual-console-state.md 4.5
//
// 设计要点：
//   - 仅解析 DSR CPR 响应（CSI row;col R）和 DA 响应（CSI ? Ps c），不做完整 VT 解析
//   - WT 的其他输出（程序输出的 VT 字节）原样转发，不经过解析
//   - 解析到 DSR CPR 或 DA 时通过回调通知调用方
#pragma once

#include <cstdint>
#include <functional>
#include <string>

namespace terminjector {

class VtParser {
public:
    VtParser() = default;

    // 输入 WT 字节流，识别到 DSR CPR 或 DA 时回调
    void Feed(const uint8_t* data, size_t len);

    // 设置光标报告回调（col, row 是 VT 1-based 坐标）
    void SetCursorReportCallback(std::function<void(int, int)> cb) {
        m_cursorCb = std::move(cb);
    }

    // Phase 15：设置 DA 报告回调（caps 为终端能力标识）
    void SetDaReportCallback(std::function<void(int)> cb) {
        m_daCb = std::move(cb);
    }

private:
    // 尝试从 m_pending 中解析 VT 响应
    // 支持格式：
    //   DSR CPR: \x1b[row;colR
    //   DA:      \x1b[?1;Psc 或 \x1b[?c
    // 匹配到完整序列后移除已匹配部分，调用对应回调
    void TryParse();

    std::string m_pending;  // 累积未识别字节
    std::function<void(int, int)> m_cursorCb;
    std::function<void(int)> m_daCb;
};

} // namespace terminjector