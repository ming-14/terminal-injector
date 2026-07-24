// 全局日志器接口（异步：调用方入队，后台线程写文件）
// 线程安全，Hook 内可安全调用（用 SRWLOCK 而非 std::mutex，避免 CRT/Hook 干扰）
// 详见 docs/phases/01-scaffold.md 4.3.2
//
// 异步设计要点：
//   - 调用方线程：格式化 → OutputDebugString → 入队（SRWLOCK 短暂加锁，无磁盘 I/O）
//   - 后台 worker 线程：出队批量 → WriteFile（无每条 flush）
//   - 高频 Hook 路径（ReadConsoleW/WriteConsoleW 等）无磁盘 I/O 阻塞
//   - 日志走两路：OutputDebugString（DebugView 实时查看）+ 独立文件句柄
//   - 文件句柄用 CreateFileW 直接打开，不走 CRT
//   - SRWLOCK + CONDITION_VARIABLE 是 kernel32 原生，不会被本项目 Hook
#pragma once

#include "LogLevel.h"
#include <string>

namespace terminjector {

class Logger {
public:
    // 初始化日志文件（进程启动时调用一次）
    // logPath 为空则仅 OutputDebugString
    // minLevel 低于此级别的日志不输出
    static void Initialize(const std::wstring& logPath,
                           LogLevel minLevel = LogLevel::Info);

    // 关闭日志（进程退出时调用）
    static void Shutdown();

    // 写日志（线程安全，可变参数 printf 风格）
    static void Log(LogLevel level, const char* fmt, ...);

    // 便捷静态方法
    static void Trace(const char* fmt, ...);
    static void Debug(const char* fmt, ...);
    static void Info(const char* fmt, ...);
    static void Warn(const char* fmt, ...);
    static void Error(const char* fmt, ...);
    static void Fatal(const char* fmt, ...);

    // 查询当前是否已初始化
    static bool IsInitialized();

    // 获取日志文件句柄（供 HandleRegistry 注册为 protected，Phase 9 用）
    static void* GetFileHandle();
};

} // namespace terminjector

// 便捷宏，自动带上文件名与行号
// 使用方式：LOG_INFO("pid=%u started", pid);
#define LOG_TRACE(fmt, ...) ::terminjector::Logger::Trace("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_DEBUG(fmt, ...) ::terminjector::Logger::Debug("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_INFO(fmt, ...)  ::terminjector::Logger::Info("[%s:%d] " fmt,  __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_WARN(fmt, ...)  ::terminjector::Logger::Warn("[%s:%d] " fmt,  __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_ERROR(fmt, ...) ::terminjector::Logger::Error("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
#define LOG_FATAL(fmt, ...) ::terminjector::Logger::Fatal("[%s:%d] " fmt, __FILE__, __LINE__, ##__VA_ARGS__)
