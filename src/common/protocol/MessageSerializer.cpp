// 消息序列化/反序列化实现
// 详见 docs/phases/01-scaffold.md 4.5.4
//
// 关键点：
//   - Serialize 直接 memcpy，不做字节序转换（x64 小端序原生匹配）
//   - Deserialize 严格校验 magic/version/length，防损坏帧
//   - RecvPacket 在传输层之上做分帧，循环 Recv 直到收齐
#include "MessageSerializer.h"

#include "../transport/ITransport.h"
#include "../logging/Logger.h"

#include <cstring>
#include <algorithm>

namespace terminjector::protocol {

std::vector<uint8_t> Serialize(MessageType type,
                               const void* payload,
                               uint32_t len) {
    std::vector<uint8_t> buf(sizeof(PacketHeader) + len);

    PacketHeader hdr{};
    hdr.magic    = kMagic;
    hdr.version  = kVersion;
    hdr.reserved = 0;
    hdr.type     = static_cast<uint32_t>(type);
    hdr.length   = len;

    std::memcpy(buf.data(), &hdr, sizeof(hdr));
    if (len > 0 && payload != nullptr) {
        std::memcpy(buf.data() + sizeof(hdr), payload, len);
    }
    return buf;
}

int Deserialize(const uint8_t* data, size_t len,
                MessageType& outType,
                std::vector<uint8_t>& outPayload) {
    if (data == nullptr) return -1;

    // 数据不足以容纳 header
    if (len < sizeof(PacketHeader)) {
        return 0;
    }

    PacketHeader hdr;
    std::memcpy(&hdr, data, sizeof(hdr));

    // 校验 magic
    if (hdr.magic != kMagic) {
        LOG_ERROR("Deserialize: magic mismatch, got=0x%08X expect=0x%08X",
                  hdr.magic, kMagic);
        return -1;
    }
    // 校验版本（主版本一致即可，这里版本号只有 1）
    if (hdr.version != kVersion) {
        LOG_ERROR("Deserialize: version mismatch, got=%u expect=%u",
                  hdr.version, kVersion);
        return -1;
    }
    // 校验 length 上限
    if (hdr.length > kMaxPayloadLen) {
        LOG_ERROR("Deserialize: payload length %u exceeds max %u",
                  hdr.length, kMaxPayloadLen);
        return -1;
    }

    const size_t totalNeeded = sizeof(PacketHeader) + hdr.length;
    if (len < totalNeeded) {
        // 数据不足，等待更多
        return 0;
    }

    outType = static_cast<MessageType>(hdr.type);
    outPayload.assign(data + sizeof(PacketHeader),
                      data + totalNeeded);
    return static_cast<int>(totalNeeded);
}

bool RecvPacket(ITransport* transport,
                MessageType& outType,
                std::vector<uint8_t>& outPayload) {
    if (transport == nullptr || !transport->IsConnected()) {
        return false;
    }

    // 先读 header
    PacketHeader hdr;
    uint8_t* hdrBytes = reinterpret_cast<uint8_t*>(&hdr);
    size_t got = 0;
    while (got < sizeof(hdr)) {
        int n = transport->Recv(hdrBytes + got, sizeof(hdr) - got);
        if (n <= 0) {
            LOG_DEBUG("RecvPacket: header Recv returned %d", n);
            return false;
        }
        got += static_cast<size_t>(n);
    }

    if (hdr.magic != kMagic) {
        LOG_ERROR("RecvPacket: magic mismatch 0x%08X", hdr.magic);
        return false;
    }
    if (hdr.version != kVersion) {
        LOG_ERROR("RecvPacket: version mismatch %u", hdr.version);
        return false;
    }
    if (hdr.length > kMaxPayloadLen) {
        LOG_ERROR("RecvPacket: payload too large %u", hdr.length);
        return false;
    }

    // 再读 payload
    outPayload.resize(hdr.length);
    got = 0;
    while (got < hdr.length) {
        int n = transport->Recv(outPayload.data() + got,
                                hdr.length - got);
        if (n <= 0) {
            LOG_DEBUG("RecvPacket: payload Recv returned %d", n);
            return false;
        }
        got += static_cast<size_t>(n);
    }

    outType = static_cast<MessageType>(hdr.type);
    return true;
}

} // namespace terminjector::protocol
