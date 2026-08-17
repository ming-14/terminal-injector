# Phase 1: 工程脚手架与依赖准备

> 本 Phase 搭建整个项目的工程骨架，是后续所有 Phase 的基础。完成后应能编译出空的 `terminal-injector.exe` 和 `injected.dll`，并具备日志、IPC 抽象、消息协议三大基础设施。

---

## 1. Phase 目标

1. 建立 CMake 工程结构，配置 MSVC 14.51 工具链，编译 x64
2. 集成 MinHook（镜像下载源码到 `third_party/`）
3. 实现 Logger 模块（`OutputDebugString` + 文件双路，线程安全，Hook 内可用）
4. 定义 `ITransport` 抽象接口 + `NamedPipeTransport` 实现
5. 定义 DLL↔中介的消息协议（消息类型、序列化、包头）
6. 定义 Console 结构体与常量（`#pragma pack` 对齐）
7. 验证：编译通过，Logger 能写日志，命名管道能双向收发字节

---

## 2. 前置依赖

- 无代码依赖（本 Phase 是起点）
- 环境依赖：MSVC 14.51 已安装、CMake ≥ 3.20、Windows SDK 10

---

## 3. 涉及文件清单

```
terminal-injector/
├── CMakeLists.txt                     # 新建：顶层 CMake
├── cmake/
│   ├── MSVCSetup.cmake                # 新建：cl 路径配置
│   ├── CompilerFlags.cmake            # 新建：编译选项
│   └── FindMinHook.cmake              # 新建：MinHook 查找
├── src/
│   ├── common/
│   │   ├── logging/
│   │   │   ├── LogLevel.h
│   │   │   ├── Logger.h
│   │   │   └── Logger.cpp
│   │   ├── transport/
│   │   │   ├── ITransport.h
│   │   │   ├── NamedPipeTransport.h
│   │   │   ├── NamedPipeTransport.cpp
│   │   │   └── TransportFactory.h
│   │   ├── protocol/
│   │   │   ├── PacketDefs.h
│   │   │   ├── Message.h
│   │   │   ├── MessageSerializer.h
│   │   │   └── MessageSerializer.cpp
│   │   └── console/
│   │       ├── ConsoleTypes.h
│   │       └── ConsoleConstants.h
│   ├── injector/
│   │   ├── Injector.h                 # 空声明（Phase 2 实现）
│   │   └── Injector.cpp
│   ├── mediator/
│   │   ├── Mediator.h                 # 空声明（Phase 2 实现）
│   │   └── Mediator.cpp
│   ├── dll/
│   │   ├── dllmain.cpp                # 空 DllMain（Phase 3 实现 Hook）
│   │   ├── HookManager.h              # 空声明
│   │   └── HookManager.cpp
│   └── app/
│       └── main.cpp                   # 双模式入口骨架（Phase 2 填充）
└── third_party/
    └── minhook/                       # 下载的源码
```

---

## 4. 详细任务

### 4.1 CMake 工程搭建

#### 4.1.1 顶层 `CMakeLists.txt`

```cmake
cmake_minimum_required(VERSION 3.20)
project(terminal_injector VERSION 0.1.0 LANGUAGES CXX)

# C++ 标准
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# 架构固定 x64
set(CMAKE_GENERATOR_PLATFORM x64)

# 加载工具链配置
list(APPEND CMAKE_MODULE_PATH "${CMAKE_SOURCE_DIR}/cmake")
include(MSVCSetup)
include(CompilerFlags)
include(FindMinHook)

# 子模块
add_subdirectory(src/common)
add_subdirectory(src/injector)
add_subdirectory(src/mediator)
add_subdirectory(src/dll)
add_subdirectory(src/app)
```

#### 4.1.2 `cmake/MSVCSetup.cmake`

配置 cl.exe 路径（不硬编码本机路径，vswhere 探测 + CACHE 覆盖）：

```cmake
# 优先用 -DTERMINJECTOR_MSVC_BASE=... 显式指定；
# 未指定时 vswhere 探测 VS 安装目录，取 VC/Tools/MSVC 下最新版本
set(TERMINJECTOR_MSVC_BASE "" CACHE PATH "MSVC 工具链根目录（留空时自动 vswhere 探测）")
# 探测: vswhere -latest -products "*" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64
#       -property installationPath → <vsroot>/VC/Tools/MSVC/<最新版本>
# 校验: 存在 <base>/bin/Hostx64/x64/cl.exe，否则 WARNING 提示手动指定
message(STATUS "MSVC base: ${TERMINJECTOR_MSVC_BASE}")
```

#### 4.1.3 `cmake/CompilerFlags.cmake`

```cmake
# 编译选项
add_compile_options(
    /W4          # 高警告级别
    /utf-8       # 源码与执行字符集 UTF-8（中文注释/字符串必需）
    /permissive- # 严格标准
    /Zc:__cplusplus  # 正确上报 __cplusplus 宏
    /EHsc        # C++ 异常
    /Wv:18       # 仅用 VS18 支持的 API
)

# Debug 额外
if(CMAKE_BUILD_TYPE STREQUAL "Debug")
    add_compile_options(/Zi /Od /MDd)
else()
    add_compile_options(/O2 /MD)
endif()

# 链接选项
add_link_options(/SUBSYSTEM:CONSOLE)
```

#### 4.1.4 `cmake/FindMinHook.cmake`

```cmake
# 查找 MinHook 源码，若不存在则镜像下载
find_path(MINHOOK_SOURCE_DIR
    NAMES MinHook.h
    PATHS "${CMAKE_SOURCE_DIR}/third_party/minhook/include"
    NO_DEFAULT_PATH
)

if(NOT MINHOOK_SOURCE_DIR)
    message(STATUS "MinHook 未找到，准备镜像下载...")
    # 下载逻辑见 4.2，这里先提示手动放置
    message(FATAL_ERROR "请先下载 MinHook 到 third_party/minhook/，见 docs/phases/01-scaffold.md 4.2 节")
endif()

message(STATUS "MinHook found: ${MINHOOK_SOURCE_DIR}")
```

### 4.2 MinHook 集成（镜像下载）

遵循 `.agents/skills/download-by-mirror/SKILL.md`，使用 GitHub 镜像，**不修改全局 git 配置**。

#### 4.2.1 下载脚本（PowerShell，手动执行一次）

```powershell
# 在项目根目录执行
$ErrorActionPreference = "Stop"

# 方式 A：下载 zip
$zipUrl = "https://v4.gh-proxy.org/https://github.com/TsudaKageyu/minhook/archive/refs/heads/master.zip"
Invoke-WebRequest -Uri $zipUrl -OutFile "minhook.zip"
Expand-Archive -Path "minhook.zip" -DestinationPath "third_party_tmp"
Move-Item "third_party_tmp/minhook-master" "third_party/minhook"
Remove-Item "minhook.zip", "third_party_tmp" -Recurse -Force

# 方式 B：git clone（仅临时环境变量，用完即清）
$env:GIT_CONFIG_COUNT = "1"
$env:GIT_CONFIG_KEY_0 = "url.https://v4.gh-proxy.org/https://github.com/.insteadOf"
$env:GIT_CONFIG_VALUE_0 = "https://github.com/"
git clone https://github.com/TsudaKageyu/minhook.git third_party/minhook
Remove-Item Env:GIT_CONFIG_COUNT, Env:GIT_CONFIG_KEY_0, Env:GIT_CONFIG_VALUE_0
```

#### 4.2.2 MinHook 作为静态库编译

在 `src/common/CMakeLists.txt`（或独立 `third_party/CMakeLists.txt`）中：

```cmake
# MinHook 静态库
add_library(minhook STATIC
    third_party/minhook/src/buffer.c
    third_party/minhook/src/hook.c
    third_party/minhook/src/trampoline.c
    third_party/minhook/src/hde/hde64.c
)
target_include_directories(minhook PUBLIC third_party/minhook/include)
```

### 4.3 Logger 模块

**设计要点**（来自之前讨论的"血泪教训"）：
- Hook 内**绝对禁止**调用任何 Console API（`WriteFile`/`WriteConsoleW` 等）
- 日志走两路：`OutputDebugString`（DebugView/sym 实时查看）+ 独立文件句柄
- 文件句柄在进程启动时打开，**不缓存到任何可能被 Hook 的对象**
- 线程安全：用轻量自旋锁（`SRWLOCK`，kernel32 原生，不会被 Hook）

#### 4.3.1 `LogLevel.h`

```cpp
#pragma once
// 日志级别定义

namespace terminjector {

enum class LogLevel : int {
    Trace = 0,
    Debug = 1,
    Info  = 2,
    Warn  = 3,
    Error = 4,
    Fatal = 5
};

// 转为字符串前缀
const char* ToString(LogLevel level);

} // namespace terminjector
```

#### 4.3.2 `Logger.h`

```cpp
#pragma once
#include "LogLevel.h"
#include <string>

namespace terminjector {

// 全局日志器（进程级单例）
// 线程安全，Hook 内可安全调用
class Logger {
public:
    // 初始化日志文件（进程启动时调用一次）
    // logPath 为空则仅 OutputDebugString
    static void Initialize(const std::wstring& logPath, LogLevel minLevel = LogLevel::Info);

    // 关闭日志
    static void Shutdown();

    // 写日志（线程安全）
    static void Log(LogLevel level, const char* fmt, ...);

    // 便捷宏
    static void Trace(const char* fmt, ...);
    static void Debug(const char* fmt, ...);
    static void Info(const char* fmt, ...);
    static void Warn(const char* fmt, ...);
    static void Error(const char* fmt, ...);
    static void Fatal(const char* fmt, ...);
};

} // namespace terminjector

// 便捷宏，自动带上文件名行号
#define LOG_TRACE(fmt, ...) ::terminjector::Logger::Trace("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_DEBUG(fmt, ...) ::terminjector::Logger::Debug("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_INFO(fmt, ...)  ::terminjector::Logger::Info("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_WARN(fmt, ...)  ::terminjector::Logger::Warn("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_ERROR(fmt, ...) ::terminjector::Logger::Error("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_FATAL(fmt, ...) ::terminjector::Logger::Fatal("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
```

#### 4.3.3 `Logger.cpp` 关键实现要点

```cpp
#include "Logger.h"
#include <windows.h>
#include <cstdio>
#include <cstdarg>
#include <mutex>

namespace terminjector {

// 用 SRWLOCK 而非 std::mutex，避免 C++ 运行时依赖与潜在 Hook
static SRWLOCK g_lock = SRWLOCK_INIT;
static HANDLE  g_fileHandle = INVALID_HANDLE_VALUE;
static LogLevel g_minLevel = LogLevel::Info;

void Logger::Initialize(const std::wstring& logPath, LogLevel minLevel) {
    g_minLevel = minLevel;
    if (!logPath.empty()) {
        // 用 CreateFileW 直接打开，不走 CRT（避免 std::ofstream 内部可能依赖其他 API）
        g_fileHandle = CreateFileW(
            logPath.c_str(),
            GENERIC_WRITE,
            FILE_SHARE_READ,
            nullptr,
            CREATE_ALWAYS,
            FILE_ATTRIBUTE_NORMAL,
            nullptr);
    }
}

void Logger::Log(LogLevel level, const char* fmt, ...) {
    if (static_cast<int>(level) < static_cast<int>(g_minLevel)) return;

    // 格式化
    char buf[2048];
    int prefixLen = std::snprintf(buf, sizeof(buf), "[%s] ", ToString(level));
    va_list args;
    va_start(args, fmt);
    int bodyLen = std::vsnprintf(buf + prefixLen, sizeof(buf) - prefixLen - 2, fmt, args);
    va_end(args);
    int totalLen = prefixLen + (bodyLen < 0 ? 0 : bodyLen);
    buf[totalLen++] = '\n';
    buf[totalLen] = '\0';

    // OutputDebugString（W 版本，UTF-8 → UTF-16）
    // 注意：OutputDebugStringW 不会被我们 Hook，安全
    wchar_t wbuf[4096];
    int wlen = MultiByteToWideChar(CP_UTF8, 0, buf, totalLen, wbuf, 4096);
    if (wlen > 0) OutputDebugStringW(wbuf);

    // 文件写入（SRWLOCK 保护）
    AcquireSRWLockExclusive(&g_lock);
    if (g_fileHandle != INVALID_HANDLE_VALUE) {
        DWORD written = 0;
        WriteFile(g_fileHandle, buf, totalLen, &written, nullptr);
        // 注意：WriteFile 写文件句柄，不是 Console 句柄，不会被 Hook 拦截
        // 但 Phase 9 的 CloseHandle Hook 需排除此句柄（Phase 9 处理）
        FlushFileBuffers(g_fileHandle);
    }
    ReleaseSRWLockExclusive(&g_lock);
}

} // namespace terminjector
```

**风险提示**：Phase 9 Hook `WriteFile` 时，必须判断句柄是否为日志文件句柄，跳过拦截。日志文件句柄注册到全局表供 Hook 查询。

### 4.4 ITransport 抽象与 NamedPipe 实现

#### 4.4.1 `ITransport.h`

```cpp
#pragma once
#include <cstdint>
#include <cstddef>
#include <string>

namespace terminjector {

// IPC 传输抽象接口
// 实现：NamedPipeTransport（Phase 1）、SharedMemoryTransport（Phase 10 扩展）
class ITransport {
public:
    virtual ~ITransport() = default;

    // 连接到对端（中介侧：连服务端；DLL 侧：连客户端）
    // 返回 true 成功
    virtual bool Connect() = 0;

    // 断开
    virtual void Disconnect() = 0;

    // 是否已连接
    virtual bool IsConnected() const = 0;

    // 发送字节流（阻塞直到全部发出或失败）
    // 返回实际发送字节数，<0 表示错误
    virtual int Send(const void* data, size_t len) = 0;

    // 接收字节流（阻塞直到读到 len 字节或失败）
    // 返回实际接收字节数，0 表示对端关闭，<0 表示错误
    virtual int Recv(void* buf, size_t len) = 0;

    // 非阻塞 peek（可选实现，默认返回 -1 表示不支持）
    virtual int Peek(void* buf, size_t len) { return -1; }
};

// 命名管道名称约定：\\.\pipe\terminjector_<target_pid>
std::wstring MakePipeName(uint32_t targetPid);

} // namespace terminjector
```

#### 4.4.2 `NamedPipeTransport.h`

```cpp
#pragma once
#include "ITransport.h"
#include <windows.h>

namespace terminjector {

// 命名管道传输实现
// 中介程序作为服务端（CreateNamedPipe），DLL 作为客户端（CreateFile）
class NamedPipeTransport : public ITransport {
public:
    enum class Role { Server, Client };

    NamedPipeTransport(const std::wstring& pipeName, Role role);
    ~NamedPipeTransport() override;

    bool Connect() override;     // Server: 创建并等待客户端连接；Client: 连接服务端
    void Disconnect() override;
    bool IsConnected() const override;
    int Send(const void* data, size_t len) override;
    int Recv(void* buf, size_t len) override;
    int Peek(void* buf, size_t len) override;

private:
    std::wstring m_pipeName;
    Role         m_role;
    HANDLE       m_pipeHandle = INVALID_HANDLE_VALUE;
};

} // namespace terminjector
```

#### 4.4.3 `NamedPipeTransport.cpp` 关键点

- **Server 端**（中介）：`CreateNamedPipeW` 创建管道 → `ConnectNamedPipe` 等待客户端
- **Client 端**（DLL）：`CreateFileW` 打开 `\\.\pipe\terminjector_<pid>`
- 使用**字节模式**（`PIPE_TYPE_BYTE | PIPE_READMODE_BYTE`），不使用消息模式（我们自己分帧）
- 缓冲区大小：输入 64KB，输出 64KB（鼠标攒批可能产生较大包）
- **超时**：`Connect` 用 `NMPWAIT_USE_DEFAULT_WAIT`；收发用阻塞 + 中介侧独立线程

```cpp
bool NamedPipeTransport::Connect() {
    if (m_role == Role::Server) {
        // 中介创建管道
        m_pipeHandle = CreateNamedPipeW(
            m_pipeName.c_str(),
            PIPE_ACCESS_DUPLEX,
            PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT,
            1,            // 仅允许 1 个客户端实例
            65536,        // 输出缓冲
            65536,        // 输入缓冲
            0,            // 默认超时
            nullptr);     // 默认安全属性
        if (m_pipeHandle == INVALID_HANDLE_VALUE) return false;

        // 等待 DLL 连接
        if (!ConnectNamedPipe(m_pipeHandle, nullptr) &&
            GetLastError() != ERROR_PIPE_CONNECTED) {
            CloseHandle(m_pipeHandle);
            m_pipeHandle = INVALID_HANDLE_VALUE;
            return false;
        }
        return true;
    } else {
        // DLL 连接
        while (true) {
            m_pipeHandle = CreateFileW(
                m_pipeName.c_str(),
                GENERIC_READ | GENERIC_WRITE,
                0, nullptr,
                OPEN_EXISTING,
                0, nullptr);
            if (m_pipeHandle != INVALID_HANDLE_VALUE) return true;
            if (GetLastError() != ERROR_PIPE_BUSY) return false;
            WaitNamedPipeW(m_pipeName.c_str(), 5000);
        }
    }
}
```

#### 4.4.4 `TransportFactory.h`

```cpp
#pragma once
#include "ITransport.h"
#include <memory>

namespace terminjector {

// 工厂，后续可扩展 SharedMemory 实现
enum class TransportType { NamedPipe /*, SharedMemory*/ };

std::unique_ptr<ITransport> CreateTransport(
    TransportType type,
    const std::wstring& pipeName,
    NamedPipeTransport::Role role);

} // namespace terminjector
```

### 4.5 消息协议（Protocol）

#### 4.5.1 设计原则

- **二进制协议**（非文本），紧凑高效
- 每个 IPC 包 = `PacketHeader` + `Payload`
- 使用**长度前缀分帧**（`PIPE_TYPE_BYTE` 模式下必需）
- 小端序（x64 原生）

#### 4.5.2 `PacketDefs.h`

```cpp
#pragma once
#include <cstdint>

namespace terminjector::protocol {

// 包头魔数，用于识别协议
constexpr uint32_t kMagic = 0x544A494E; // 'TJIN' (Terminal INjector)

// 协议版本
constexpr uint16_t kVersion = 1;

// 包头（16 字节，8 字节对齐）
#pragma pack(push, 8)
struct PacketHeader {
    uint32_t magic;    // kMagic
    uint16_t version;  // kVersion
    uint16_t reserved; // 保留，填 0
    uint32_t type;     // MessageType 枚举值
    uint32_t length;   // payload 字节数（不含 header）
};
#pragma pack(pop)
static_assert(sizeof(PacketHeader) == 16, "PacketHeader 必须为 16 字节");

} // namespace terminjector::protocol
```

#### 4.5.3 `Message.h`

```cpp
#pragma once
#include <cstdint>

namespace terminjector::protocol {

// 消息类型枚举
enum class MessageType : uint32_t {
    // 握手（Phase 2）
    Hello        = 0x0001,  // DLL 注入成功后上报：含目标 PID、初始状态快照
    HelloAck     = 0x0002,  // 中介确认

    // 输出数据（Phase 4）
    VtOutput     = 0x0010,  // 已翻译的 VT 序列字节流

    // 输入数据（Phase 6）
    VtInput      = 0x0020,  // WT 发来的 VT 输入字节流

    // 控制流（Phase 5/7）
    ResizeNotify = 0x0030,  // 中介→DLL：WT 窗口尺寸变化
    ModeChange   = 0x0031,  // DLL→中介：目标程序切换 ConsoleMode
    CpChange     = 0x0032,  // DLL→中介：目标程序切换 ConsoleCP

    // 心跳/保活（Phase 10）
    Ping         = 0x0040,
    Pong         = 0x0041,

    // 卸载（Phase 11）
    Shutdown     = 0x0050,  // 中介→DLL：要求卸载
    ByeAck       = 0x0051,  // DLL→中介：已卸载
};

// 各消息的 payload 结构（按需定义，此处先列骨架）

#pragma pack(push, 1)
struct HelloPayload {
    uint32_t targetPid;
    uint32_t targetBitness;   // 32 / 64
    uint16_t consoleMode;     // 初始 GetConsoleMode
    uint16_t consoleCp;       // 初始 GetConsoleCP
    uint16_t consoleOutputCp; // 初始 GetConsoleOutputCP
    uint16_t bufferCols;      // 初始缓冲区宽
    uint16_t bufferRows;      // 初始缓冲区高
    uint16_t cursorX;
    uint16_t cursorY;
    uint16_t reserved;
};

struct ResizePayload {
    uint16_t cols;
    uint16_t rows;
    uint16_t bufferCols;  // 屏幕缓冲区尺寸（可能 > 窗口）
    uint16_t bufferRows;
};

struct ModeChangePayload {
    uint32_t inputMode;   // CONIN$ 模式
    uint32_t outputMode;  // CONOUT$ 模式
};
#pragma pack(pop)

} // namespace terminjector::protocol
```

#### 4.5.4 `MessageSerializer.h` / `.cpp`

```cpp
#pragma once
#include "PacketDefs.h"
#include "Message.h"
#include <vector>
#include <cstdint>

namespace terminjector::protocol {

// 序列化：payload → 完整包（header + payload）
std::vector<uint8_t> Serialize(MessageType type, const void* payload, uint32_t len);

// 反序列化：从字节流中读取一个完整包
// 返回消费的字节数；0 表示数据不足；<0 表示协议错误
// outType/outPayload 输出解析结果
int Deserialize(const uint8_t* data, size_t len,
                MessageType& outType, std::vector<uint8_t>& outPayload);

} // namespace terminjector::protocol
```

**关键实现**：`Serialize` 直接 `resize(16 + len)`，填 header，memcpy payload。`Deserialize` 先检查是否 ≥ 16 字节、magic/version，再按 `header.length` 判断是否完整。

### 4.6 Console 类型与常量

#### 4.6.1 `ConsoleTypes.h`

重定义 Console 结构体，**强制 1 字节对齐**（来自之前讨论的硬性要求）：

```cpp
#pragma once
#include <windows.h>

namespace terminjector::console {

// 重定义以保证内存布局精确匹配系统
// 实际上 Windows SDK 的定义已经正确，这里主要是为了 #pragma pack 保险
#pragma pack(push, 1)

using COORD_EX      = COORD;
using SMALL_RECT_EX = SMALL_RECT;

// 继承自系统结构体，添加便利方法
struct ConsoleScreenBufferInfo : public CONSOLE_SCREEN_BUFFER_INFO {
    // 便捷访问
    SHORT Cols() const { return dwSize.X; }
    SHORT Rows() const { return dwSize.Y; }
    SHORT WinWidth() const { return srWindow.Right - srWindow.Left + 1; }
    SHORT WinHeight() const { return srWindow.Bottom - srWindow.Top + 1; }
};

#pragma pack(pop)

} // namespace terminjector::console
```

#### 4.6.2 `ConsoleConstants.h`

```cpp
#pragma once
#include <windows.h>

namespace terminjector::console {

// Console Mode 标志位（与 Windows SDK 一致，集中引用）
constexpr DWORD kInputModeDefault =
    ENABLE_ECHO_INPUT | ENABLE_LINE_INPUT | ENABLE_PROCESSED_INPUT |
    ENABLE_EXTENDED_FLAGS | ENABLE_INSERT_MODE | ENABLE_QUICK_EDIT_MODE;

constexpr DWORD kOutputModeVt =
    ENABLE_PROCESSED_OUTPUT | ENABLE_WRAP_AT_EOL_OUTPUT |
    ENABLE_VIRTUAL_TERMINAL_PROCESSING;

// 代码页
constexpr UINT kCpUtf8 = 65001;
constexpr UINT kCpGbk  = 936;
constexpr UINT kCpDefault = kCpUtf8;

} // namespace terminjector::console
```

### 4.7 各模块 CMakeLists

#### `src/common/CMakeLists.txt`

```cmake
add_library(terminjector_common STATIC
    logging/Logger.cpp
    transport/NamedPipeTransport.cpp
    protocol/MessageSerializer.cpp
)
target_include_directories(terminjector_common PUBLIC .)
target_link_libraries(terminjector_common PUBLIC minhook)
```

#### `src/dll/CMakeLists.txt`

```cmake
add_library(injected_dll SHARED
    dllmain.cpp
    HookManager.cpp
)
target_link_libraries(injected_dll PRIVATE terminjector_common)
set_target_properties(injected_dll PROPERTIES OUTPUT_NAME "injected")
```

#### `src/app/CMakeLists.txt`

```cmake
add_executable(terminal_injector main.cpp)
target_link_libraries(terminal_injector PRIVATE
    terminjector_common
    # injector / mediator 库在 Phase 2 补
)
```

### 4.8 空 `main.cpp` 骨架（Phase 2 填充）

```cpp
#include "logging/Logger.h"
#include <cstdio>
#include <windows.h>

// 双模式入口（Phase 2 实现）
// terminal-injector.exe --inject <pid>
// terminal-injector.exe --mediator [--pipe <name>]

int main(int argc, char* argv[]) {
    using namespace terminjector;
    Logger::Initialize(L"terminal-injector.log", LogLevel::Debug);
    LOG_INFO("terminal-injector starting, argc=%d", argc);

    // Phase 2 在此解析参数并分发到 injector/mediator

    LOG_INFO("terminal-injector exit");
    Logger::Shutdown();
    return 0;
}
```

### 4.9 空 `dllmain.cpp`（Phase 3 实现 Hook）

```cpp
#include <windows.h>
#include "logging/Logger.h"

// Phase 3 在此实现：DisableThreadLibraryCalls + MH_Initialize + 懒加载
BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        // 日志目录：TI_INJECTED_LOG_DIR 优先，否则 GetTempPathW()（不硬编码固定路径）
        terminjector::Logger::Initialize(terminjector::GetInjectedLogDir() + L"\\injected.log",
                                         terminjector::LogLevel::Debug);
        LOG_INFO("injected.dll loaded");
    } else if (reason == DLL_PROCESS_DETACH) {
        LOG_INFO("injected.dll unloaded");
        terminjector::Logger::Shutdown();
    }
    return TRUE;
}
```

---

## 5. 验证标准（Phase 1 完成判定）

### 5.1 编译验证

```powershell
# 在项目根目录
cmake -B build -G "Ninja" -DCMAKE_BUILD_TYPE=Debug
cmake --build build
```

预期产出：
- `build/src/app/terminal_injector.exe`（可运行，打印日志后退出）
- `build/src/dll/injected.dll`（可被 LoadLibrary 加载）
- `build/third_party/minhook/minhook.lib`（静态库）

### 5.2 Logger 验证

运行 `terminal_injector.exe`，检查：
- 当前目录生成 `terminal-injector.log`，含 `terminal-injector starting` 与 `exit` 行
- 打开 DebugView（或 `windows-debugging` 工具），能看到对应日志输出

### 5.3 ITransport 验证

写一个临时测试程序（不入库，手动验证）：
- 启动两个进程，一个作为 Server，一个作为 Client
- Client 发送 `Hello`，Server 收到并回 `HelloAck`
- 验证 `Serialize`/`Deserialize` 正确分帧

### 5.4 MinHook 集成验证

在 `main.cpp` 临时加一段：
```cpp
#include <MinHook.h>
// ...
MH_Initialize();
LOG_INFO("MinHook initialized, version=%s", MH_VERSION);
MH_Uninitialize();
```
确认能编译链接，运行打印 MinHook 版本号。

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| MSVC 路径未正确配置导致 CMake 找不到 cl | 用 VS Developer Command Prompt 或 `cmake -G "Visual Studio 18 2022"` |
| MinHook 镜像下载失败 | 备选 `ghproxy.com` / `gh.llkk.cc`；或手动下载 zip 放入 `third_party/` |
| `/utf-8` 编译选项缺失导致中文注释乱码 | 已在 CompilerFlags 中强制开启 |
| Logger 文件写入与 Phase 9 `WriteFile` Hook 冲突 | 全局注册日志句柄，Phase 9 Hook 中查询跳过 |
| `OutputDebugString` 在 DLL 加载早期不可用 | DllMain 中 Initialize 仅打开文件，不调用 ODS |

---

## 7. 交付物清单

- [ ] `CMakeLists.txt` 及 `cmake/` 下 3 个模块
- [ ] MinHook 源码到位（`third_party/minhook/`）
- [ ] `src/common/` 下 logging/transport/protocol/console 全部文件
- [ ] `src/app/main.cpp` 空骨架
- [ ] `src/dll/dllmain.cpp` 空骨架
- [ ] `src/injector/`、`src/mediator/` 空声明
- [ ] 编译通过，5.1 ~ 5.4 验证全过
