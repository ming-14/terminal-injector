// 日志级别定义
// 详见 docs/phases/01-scaffold.md 4.3.1
#pragma once

namespace terminjector {

// 日志级别（数值越大越严重）
enum class LogLevel : int {
    Trace = 0,  // 极细粒度，用于调试 Hook 流程
    Debug = 1,  // 调试信息
    Info  = 2,  // 关键流程节点
    Warn  = 3,  // 警告，可恢复
    Error = 4,  // 错误，影响功能但进程继续
    Fatal = 5   // 致命，进程将退出
};

// 转为短字符串前缀（用于日志行首）
inline const char* ToString(LogLevel level) {
    switch (level) {
        case LogLevel::Trace: return "TRACE";
        case LogLevel::Debug: return "DEBUG";
        case LogLevel::Info:  return "INFO ";
        case LogLevel::Warn:  return "WARN ";
        case LogLevel::Error: return "ERROR";
        case LogLevel::Fatal: return "FATAL";
    }
    return "?    ";
}

} // namespace terminjector
