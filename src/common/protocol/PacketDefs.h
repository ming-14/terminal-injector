// IPC 包头定义
// 详见 docs/phases/01-scaffold.md 4.5.2
//
// 协议设计：
//   - 二进制协议，紧凑高效
//   - 每个 IPC 包 = PacketHeader(16B) + Payload(变长)
//   - 长度前缀分帧（PIPE_TYPE_BYTE 模式下必需）
//   - 小端序（x64 原生）
#pragma once

#include <cstdint>

namespace terminjector::protocol {

// 包头魔数，用于识别协议（'TJIN' = Terminal INjector）
constexpr uint32_t kMagic = 0x544A494E;

// 协议版本
constexpr uint16_t kVersion = 1;

// 包头（16 字节，8 字节对齐保证跨进程一致）
#pragma pack(push, 8)
struct PacketHeader {
    uint32_t magic;     // kMagic
    uint16_t version;   // kVersion
    uint16_t reserved;  // 保留，填 0
    uint32_t type;      // MessageType 枚举值
    uint32_t length;    // payload 字节数（不含 header）
};
#pragma pack(pop)
static_assert(sizeof(PacketHeader) == 16, "PacketHeader 必须为 16 字节");

} // namespace terminjector::protocol
