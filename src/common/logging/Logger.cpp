// Logger 实现：外观模式，委托给 RingBufferLogger
// 详见 docs/phases/01-scaffold.md 4.3.3
//
// Phase 10 任务4.6：替换为 RingBufferLogger（thread_local ring buffer + 后台刷盘）
// 旧实现（SRWLOCK 全局队列 + std::deque）已移除
//
// 设计：
//   - Logger 是外观（Facade），保持原有静态接口不变
//   - RingBufferLogger 是实现（框架与驱动层）
//   - LOG_INFO 等宏调用 Logger 静态方法，无需修改调用点
//   - va_list 转发：Logger::Log 用 va_list 调 RingBufferLogger::LogV

#include "Logger.h"
#include "RingBufferLogger.h"

namespace terminjector {

// 全局 RingBufferLogger 单例（Meyers's Singleton，C++11 起线程安全初始化）
static RingBufferLogger& GetRingBufferLogger() {
    static RingBufferLogger inst;
    return inst;
}

// ============================================================
// Logger 静态方法：转发给 RingBufferLogger
// ============================================================

void Logger::Initialize(const std::wstring& logPath, LogLevel minLevel) {
    GetRingBufferLogger().Initialize(logPath, minLevel);
}

void Logger::Shutdown() {
    GetRingBufferLogger().Shutdown();
}

void Logger::Log(LogLevel level, const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    GetRingBufferLogger().LogV(level, fmt, args);
    va_end(args);
}

void Logger::Trace(const char* fmt, ...) {
    va_list args; va_start(args, fmt);
    GetRingBufferLogger().LogV(LogLevel::Trace, fmt, args);
    va_end(args);
}

void Logger::Debug(const char* fmt, ...) {
    va_list args; va_start(args, fmt);
    GetRingBufferLogger().LogV(LogLevel::Debug, fmt, args);
    va_end(args);
}

void Logger::Info(const char* fmt, ...) {
    va_list args; va_start(args, fmt);
    GetRingBufferLogger().LogV(LogLevel::Info, fmt, args);
    va_end(args);
}

void Logger::Warn(const char* fmt, ...) {
    va_list args; va_start(args, fmt);
    GetRingBufferLogger().LogV(LogLevel::Warn, fmt, args);
    va_end(args);
}

void Logger::Error(const char* fmt, ...) {
    va_list args; va_start(args, fmt);
    GetRingBufferLogger().LogV(LogLevel::Error, fmt, args);
    va_end(args);
}

void Logger::Fatal(const char* fmt, ...) {
    va_list args; va_start(args, fmt);
    GetRingBufferLogger().LogV(LogLevel::Fatal, fmt, args);
    va_end(args);
}

bool Logger::IsInitialized() {
    return GetRingBufferLogger().IsInitialized();
}

void* Logger::GetFileHandle() {
    return GetRingBufferLogger().GetFileHandle();
}

} // namespace terminjector
