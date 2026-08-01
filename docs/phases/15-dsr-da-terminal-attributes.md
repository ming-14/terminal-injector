# Phase 15: DSR/DA 终端属性查询

> 本 Phase 实现 WT 光标位置查询（DSR CPR）和终端能力标识获取（Primary DA），使 DLL 的 VirtualConsoleState 能从 WT 反向同步真实状态。

---

## 1. Phase 目标

1. **DSR CPR 查询**：注入后发送 `\x1b[6n` 查询 WT 当前光标位置，通过 mediator VtParser 解析响应，更新 VirtualConsoleState
2. **Primary DA 查询**：注入后发送 `\x1b[c` 查询 WT 终端能力标识，记录终端类型（如 VT320/VT525）
3. **VtParser 解析器**：mediator 侧轻量 VT 解析器，识别 DSR CPR 和 DA 响应
4. **WtStateReport 消息**：mediator → DLL 的状态报告协议，传递光标位置和终端能力

---

## 2. 实现细节

### 2.1 LazyInit 发送查询

```cpp
// LazyInit.cpp
// Phase 15：发送 DSR CPR 查询校准 WT 真实光标位置
hooks::SendToMediator(vt::kDsrCprQuery, strlen(vt::kDsrCprQuery),
                      protocol::MessageType::VtOutput);

// Phase 15：发送 Primary DA 查询获取终端能力标识
hooks::SendToMediator(vt::kDaPrimaryQuery, strlen(vt::kDaPrimaryQuery),
                      protocol::MessageType::VtOutput);
```

### 2.2 Mediator VtParser 解析

```cpp
// Mediator.cpp BridgeLoop
// Phase 14：设置 VtParser DSR CPR 回调
m_vtParser.SetCursorReportCallback([this](int col, int row) {
    protocol::WtStateReportPayload wt{};
    wt.type = 1;  // cursor_report
    wt.cols = col;
    wt.rows = row;
    auto pkt = protocol::Serialize(protocol::MessageType::WtStateReport, &wt, sizeof(wt));
    m_transport->Send(pkt.data(), pkt.size());
});

// Phase 15：设置 VtParser DA 报告回调
m_vtParser.SetDaReportCallback([this](int caps) {
    protocol::WtStateReportPayload wt{};
    wt.type = 2;  // da_report
    wt.cols = caps;
    // ...
});
```

### 2.3 VtParser 解析逻辑

```cpp
// VtParser.cpp
// DSR CPR 响应格式：\x1b[row;colR
// DA 响应格式：\x1b[?1;Psc 或 \x1b[?c
void VtParser::TryParse() {
    // 检查 DSR CPR 响应：\x1b[row;colR
    // 检查 DA 响应：\x1b[? 开头
    // 提取参数并调用对应回调
}
```

### 2.4 DLL 侧 VirtualConsoleState 更新

```cpp
// DllRecvLoop.cpp 中处理 WtStateReport
case protocol::MessageType::WtStateReport: {
    auto wt = reinterpret_cast<const protocol::WtStateReportPayload*>(payload.data());
    if (wt->type == 1) {
        // Phase 14：DSR CPR 光标报告
        VirtualConsoleState::Instance().ApplyWtCursorReport(wt->cols, wt->rows);
    } else if (wt->type == 2) {
        // Phase 15：DA 终端能力报告
        VirtualConsoleState::Instance().ApplyWtDaReport(wt->cols);
    }
    break;
}
```

---

## 3. 涉及文件

```
src/dll/
├── LazyInit.cpp                    # 发送 DSR CPR 和 DA 查询
├── DllRecvLoop.cpp                 # 处理 WtStateReport 消息
└── state/
    └── VirtualConsoleState.h/.cpp  # ApplyWtDaReport
src/mediator/
├── Mediator.cpp                    # 设置 DA 回调
├── VtParser.h                      # 轻量 VT 解析器声明
└── VtParser.cpp                    # DSR CPR + DA 响应解析
src/common/protocol/
└── Message.h                       # WtStateReport 消息类型
```

---

## 4. 测试

```
tests/runners/test_phase15.py  # DSR/DA 终端属性查询测试
```

测试内容：
- DSR CPR 查询后 VirtualConsoleState 光标位置正确
- Primary DA 查询后终端能力标识已记录
- 无查询时默认值正确