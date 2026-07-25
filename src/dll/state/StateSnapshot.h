// 注入瞬间 Console 状态快照
// 详见 docs/phases/03-dll-framework.md 4.3
//
// 关键点：
//   - Capture() 必须在 Hook 安装前调用，拿到真实 ConHost 状态
//   - ToHelloPayload() 转成协议结构，随 Hello 消息发给 mediator
//   - mediator 收到后用 VT 序列把 WT 调整到该状态（ApplySnapshotToWt）
//   - Phase 10：CaptureScreenContent 读取可见区 CHAR_INFO 矩阵，
//     握手后补发给 WT，解决注入前输出丢失导致的光标错位
//
// 这是劫持后界面不乱的关键：注入瞬间把目标当前状态"搬"到 WT
#pragma once

#include <windows.h>
#include <vector>
#include "protocol/Message.h"

namespace terminjector {

// 注入瞬间的 Console 状态快照（值类型，可拷贝）
struct StateSnapshot {
    // 屏幕缓冲区信息（含光标位置、窗口位置、尺寸、属性）
    CONSOLE_SCREEN_BUFFER_INFO screenBufferInfo{};
    // 光标显隐与大小
    CONSOLE_CURSOR_INFO   cursorInfo{};
    // 字体信息
    CONSOLE_FONT_INFOEX   fontInfo{};
    // 输入/输出模式
    DWORD inputMode = 0;
    DWORD outputMode = 0;
    // 代码页
    UINT  inputCp = 0;
    UINT  outputCp = 0;
    // 窗口标题
    wchar_t title[260] = {};
    // 窗口可见性
    BOOL  windowVisible = FALSE;

    // Phase 10：可见区屏幕内容（srWindow 区域的 CHAR_INFO 矩阵）
    // Capture() 中用 ReadConsoleOutputW 读取，握手后补发给 WT
    // screenRegion 为 VT 输出目标区域（0-based，映射 srWindow 到 WT 的 0,0）
    std::vector<CHAR_INFO> screenCells;
    SMALL_RECT screenRegion{};

    // 读取当前进程的真实 Console 状态（必须在 Hook 安装前调用）
    // 返回 false 表示读取失败（可能无 Console）
    // Phase 10：同时读取可见区屏幕内容到 screenCells
    bool Capture();

    // Phase 10：读取可见区屏幕内容（srWindow 区域）到 screenCells
    // 在 Capture() 内部调用，也可单独调用
    void CaptureScreenContent();

    // 转成 HelloPayload（随 Hello 消息发给 mediator）
    protocol::HelloPayload ToHelloPayload() const;
};

} // namespace terminjector
