// VT 透传逻辑（mediator 侧）
// 详见 docs/phases/03-dll-framework.md 4.7
//
// 负责在 WT stdin ↔ DLL pipe 之间搬运字节，不解析 VT 内容
// - ForwardStdinToPipe: WT stdin → 通过路由回调分发到活跃子进程或父进程
// - ForwardPipeToStdout: DLL pipe → 收 VtOutput → WT stdout（透传渲染）
//
// Phase 12 扩展：
//   - ForwardPipeToStdout 增加非 VtOutput 消息回调
//   - mediator 据此处理 ChildProcessNotify（创建子进程会话）
//
// Phase 12+ 输入路由扩展：
//   - ForwardStdinToPipe 改为接受 InputRouter 回调，由 mediator 决定发送目标
//   - mediator 的 RouteInput 维护活跃 ChildSession 列表，优先转发到最深前台子进程
//   - 无活跃子进程时，路由回父进程 transport（保持原有行为）
//
// 线程模型：
//   - ForwardStdinToPipe 在独立线程运行（ReadConsoleInputW 阻塞）
//   - ForwardPipeToStdout 在主线程运行（Peek 轮询）
//   - pipe 断开时主线程退出；BridgeLoop 置 stop 并从其他线程 CancelIoEx
//     唤醒 stdin 线程后 join（避免进程挂死），见 Mediator::BridgeLoop
#pragma once

#include "protocol/Message.h"

#include <atomic>
#include <functional>
#include <vector>
#include <cstdint>

namespace terminjector {

class ITransport;

class VtPassThrough {
public:
    // 非 VtOutput 消息处理器（Phase 12）
    // mediator 用此回调处理 ChildProcessNotify 等控制消息
    using NonVtMessageHandler = std::function<void(protocol::MessageType,
                                                    const std::vector<uint8_t>&)>;

    // 输入路由回调（Phase 12+）
    // 收到 WT stdin 字节后由 mediator 调用 RouteInput 决定发送目标
    // data/len: WT stdin 读到的 VT 字节流
    using InputRouter = std::function<void(const uint8_t* data, size_t len)>;

    // WT stdin → 调用 router 分发字节流（阻塞循环，stdin EOF、router 失败或 stop 置位时退出）
    // router 由 mediator 提供，内部根据活跃 ChildSession 列表路由到子进程或父进程
    // stop: BridgeLoop 请求停止（pipe 断开清理时置位）；
    //   stop 置位后外部须从其他线程 CancelIoEx(hStdin) 唤醒阻塞的
    //   ReadConsoleInputW，线程在下一次循环检查 stop 后退出
    // done: 线程退出前置位，供 BridgeLoop join 前轮询确认（配合 CancelIoEx 重试）
    static void ForwardStdinToPipe(InputRouter router,
                                   const std::atomic<bool>& stop,
                                   std::atomic<bool>& done);

    // DLL pipe → 收 VtOutput → WT stdout（阻塞循环，pipe 断开时退出）
    // handler: 非 VtOutput 消息回调（可选，nullptr 时仅记日志）
    // 返回后 BridgeLoop 结束
    static void ForwardPipeToStdout(ITransport& transport,
                                    NonVtMessageHandler handler = nullptr);
};

} // namespace terminjector
