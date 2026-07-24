// 传输层工厂
// 详见 docs/phases/01-scaffold.md 4.4.4
//
// 设计目的：
//   - 上层（中介/DLL）通过统一接口创建 ITransport，不直接 new 具体类型
//   - 后续 Phase 10 扩展 SharedMemory 实现时，只需在此添加分支
#pragma once

#include "ITransport.h"
#include "NamedPipeTransport.h"

#include <memory>

namespace terminjector {

// 传输类型枚举
enum class TransportType {
    NamedPipe,
    // SharedMemory  // Phase 10 扩展
};

// 创建传输实例
// type     传输类型
// pipeName 命名管道名称（NamedPipe 类型使用）
// role     命名管道角色（Server=中介，Client=DLL）
// 返回 ITransport 智能指针，失败返回 nullptr
inline std::unique_ptr<ITransport> CreateTransport(
    TransportType type,
    const std::wstring& pipeName,
    NamedPipeTransport::Role role) {
    switch (type) {
        case TransportType::NamedPipe:
            return std::make_unique<NamedPipeTransport>(pipeName, role);
        // case TransportType::SharedMemory:
        //     return std::make_unique<SharedMemoryTransport>(...);
    }
    return nullptr;
}

} // namespace terminjector
