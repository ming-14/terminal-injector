// 消息序列化/反序列化接口
// 详见 docs/phases/01-scaffold.md 4.5.4
//
// 协议帧格式：
//   +----------------+---------------------+
//   | PacketHeader   | Payload (变长)      |
//   | (16 字节)      | (header.length 字节)|
//   +----------------+---------------------+
//
// 使用方式（发送端）：
//   auto buf = protocol::Serialize(MessageType::Hello, &hello, sizeof(hello));
//   transport->Send(buf.data(), buf.size());
//
// 使用方式（接收端，基于 RecvPacket）：
//   MessageType type;
//   std::vector<uint8_t> payload;
//   if (protocol::RecvPacket(transport, type, payload)) { ... }
#pragma once

#include "PacketDefs.h"
#include "Message.h"

#include <cstddef>
#include <cstdint>
#include <vector>

namespace terminjector {

class ITransport;  // 前向声明，避免 include 循环

namespace protocol {

// 序列化：payload + header -> 完整包字节流
// type  消息类型
// payload 负载地址，可为 nullptr（length=0 时）
// len   负载字节数
// 返回完整包（16 + len 字节）
std::vector<uint8_t> Serialize(MessageType type,
                               const void* payload,
                               uint32_t len);

// 反序列化：从一段字节流中解析一个完整包
// data/len 输入缓冲
// outType 输出消息类型
// outPayload 输出负载（不含 header）
// 返回值：
//   >0  成功，返回消费的字节数
//   0   数据不足（还需更多字节），保持 outPayload 为空
//   <0  协议错误（magic/version 不匹配，或 length 超过上限）
int Deserialize(const uint8_t* data, size_t len,
                MessageType& outType,
                std::vector<uint8_t>& outPayload);

// 工具函数：从 ITransport 阻塞读取一个完整包
// 内部循环 Recv 直到收齐一个完整帧
// 返回 true 成功；false 表示连接断开或协议错误
bool RecvPacket(ITransport* transport,
                MessageType& outType,
                std::vector<uint8_t>& outPayload);

// 单包最大 payload 上限（防恶意/损坏的 length 字段导致超大分配）
// 4MB，足够鼠标攒批等大包
constexpr uint32_t kMaxPayloadLen = 4u * 1024 * 1024;

} // namespace protocol
} // namespace terminjector
