# Phase 11: 卸载清理与多目标测试

> 本 Phase 实现干净的卸载机制，并执行全量多目标测试。完成后，关闭 WT 标签页或主动卸载时，DLL 能解除所有 Hook、恢复目标程序原 Console 行为、释放资源。同时对 cmd/powershell/python/opencode/vim/less 等目标程序进行全面验证。

---

## 1. Phase 目标

1. **管道断开检测**：DLL 接收线程检测到 `Recv` 返回 0 或错误时，触发卸载流程
2. **卸载流程**：
   - `HookManager::UninstallAll()`（DisableHook + RemoveHook）
   - 恢复 `ConsoleState` 到真实 ConHost 状态（调 `_orig` 读取）
   - 释放 `InputQueue`、`fakeWaitHandle`、`HandleRegistry` 等资源
   - 通过 `FreeLibraryAndExitThread` 安全卸载 DLL（避免 DllMain 中 FreeLibrary 死锁）
3. **mediator 退出**：管道断开后清理 `WtSizeWatcher`、桥接线程，退出码 0
4. **多目标测试矩阵**：对 7 类目标程序逐项验证
5. **手动测试清单**与**自动化脚本**
6. 验收：所有目标程序在所有维度通过

---

## 2. 前置依赖

- Phase 10 完成（性能与稳定性达标）

---

## 3. 涉及文件清单

```
src/dll/
├── Unloader.h / .cpp                 # 新建：卸载流程
├── dllmain.cpp                       # 修改：DETACH 调用 Unloader
└── DllRecvLoop.cpp                   # 修改：检测断开触发卸载
src/mediator/
└── Mediator.cpp                      # 修改：管道断开清理
tests/
├── targets/                          # 测试目标脚本
│   ├── test_cmd.bat
│   ├── test_powershell.ps1
│   ├── test_python_tui.py
│   └── test_opencode.sh
├── runners/
│   └── run_all.py                    # 自动化测试
└── manual/
    └── checklist.md                  # 手动测试清单
```

---

## 4. 详细任务

### 4.1 管道断开检测

```cpp
// DllRecvLoop.cpp
void DllRecvLoop() {
    VtInputParser parser;
    while (g_transport->IsConnected()) {
        protocol::MessageType type;
        std::vector<uint8_t> payload;
        if (!RecvPacket(*g_transport, type, payload)) {
            LOG_WARN("Pipe disconnected, triggering unload");
            break;
        }
        // ... 处理消息 ...
    }
    // 触发卸载
    Unloader::RequestUnload();
}
```

`RecvPacket` 返回 false 的情况：
- `Recv` 返回 0（对端关闭）
- `Recv` 返回 < 0（错误）
- 协议解析失败（magic 不符等）

### 4.2 卸载流程（`Unloader`）

```cpp
// Unloader.h
#pragma once
#include <atomic>

namespace terminjector {

class Unloader {
public:
    // 请求卸载（可从任意线程调用，线程安全）
    static void RequestUnload();

    // 是否正在卸载
    static bool IsUnloading() { return s_unloading.load(); }

private:
    static void DoUnload();
    static std::atomic<bool> s_unloading;
};

} // namespace terminjector
```

```cpp
// Unloader.cpp
std::atomic<bool> Unloader::s_unloading{false};

void Unloader::RequestUnload() {
    if (s_unloading.exchange(true)) return; // 已在卸载
    // 在独立线程执行卸载，避免在 recv 线程或 Hook 线程中死锁
    std::thread([]{
        DoUnload();
    }).detach();
}

void Unloader::DoUnload() {
    LOG_INFO("Unload starting");

    // 1. 停止后台轮询
    StatePoller::Instance().Stop();

    // 2. 唤醒所有阻塞在 InputQueue 上的线程
    InputQueue::Instance().SignalDataReady();

    // 3. 卸载所有 Hook（DisableHook + RemoveHook）
    HookManager::UninstallAll();

    // 4. 恢复 ConsoleState 到真实 ConHost 状态
    //    用 _orig 读取真实状态写回（让目标程序下次查询拿到 ConHost 真值）
    HANDLE hOut = GetStdHandle_orig(STD_OUTPUT_HANDLE);
    CONSOLE_SCREEN_BUFFER_INFO info;
    if (GetConsoleScreenBufferInfo_orig(hOut, &info)) {
        ConsoleState::Instance().SetBufferSize(info.dwSize);
        ConsoleState::Instance().SetCursorPosition(info.dwCursorPosition);
        ConsoleState::Instance().SetWindow(info.srWindow);
    }

    // 5. 断开传输
    if (g_transport) {
        g_transport->Disconnect();
        g_transport.reset();
    }

    // 6. 显示原 Console 窗口（若 Phase 9 隐藏过）
    HWND hCon = GetConsoleWindow();
    if (hCon) ShowWindow(hCon, SW_SHOW);

    // 7. 发送 ByeAck 通知 mediator（若管道还能用）
    //    通常管道已断，跳过

    // 8. 关闭日志
    LOG_INFO("Unload complete, DLL ready for FreeLibrary");
    Logger::Shutdown();

    // 9. 安全卸载 DLL
    //    不能在 DllMain 中 FreeLibrary（Loader Lock）
    //    用 FreeLibraryAndExitThread：当前线程退出并卸载 DLL
    HMODULE hSelf = GetModuleHandleW(L"injected.dll");
    if (hSelf) {
        FreeLibraryAndExitThread(hSelf, 0);
        // 不会返回
    }
}
```

**关键**：`FreeLibraryAndExitThread` 是唯一安全的自卸载方式。它会：
1. 减少 DLL 引用计数
2. 触发 `DLL_PROCESS_DETACH`（DllMain）
3. 终止当前线程

### 4.3 DllMain DETACH 处理

```cpp
// dllmain.cpp
BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        // ... Phase 3 ...
    } else if (reason == DLL_PROCESS_DETACH) {
        // 此时 Hook 已卸载（Unloader::DoUnload 已执行）
        // 仅做最小清理
        MH_Uninitialize();
        // 注意：Logger 已在 DoUnload 中 Shutdown，不重复
    }
    return TRUE;
}
```

### 4.4 mediator 退出清理

```cpp
// Mediator.cpp
void Mediator::BridgeLoop() {
    // ... 既有桥接 ...
    // 管道断开或 stdin EOF 时退出循环
    LOG_INFO("BridgeLoop exit, cleaning up");
}

int Mediator::Run(...) {
    // ... 握手、BridgeLoop ...
    m_sizeWatcher.Stop();
    if (m_transport) m_transport->Disconnect();

    // 可选：等待 DLL 发 ByeAck（超时 2 秒）
    // 通常管道断开即代表 DLL 已卸载或正在卸载
    LOG_INFO("Mediator exit");
    return 0;
}
```

### 4.5 主动卸载命令（可选）

支持用户主动触发卸载（不关闭 WT）：

```
terminal-injector.exe --unload <pid>
```

实现：向目标进程发送一个卸载信号（如通过控制台事件，或再次注入一个 unload DLL）。本 Phase 仅文档记录，不实现（关闭 WT tab 即可触发卸载）。

### 4.6 多目标测试矩阵

#### 4.6.1 测试目标脚本

`tests/targets/test_cmd.bat`：
```bat
@echo off
echo === CMD Test ===
echo Plain text
color 0A
echo Green on black
color 07
dir /b
echo Line 1
echo Line 2
prompt $P$G
```

`tests/targets/test_python_tui.py`：
```python
import curses
def main(stdscr):
    curses.curs_set(0)
    stdscr.addstr(0, 0, "Python Curses Test")
    stdscr.addstr(1, 0, "Click to see coordinates")
    stdscr.refresh()
    while True:
        ch = stdscr.getch()
        if ch == ord('q'): break
        if ch == curses.KEY_MOUSE:
            _, x, y, _, _ = curses.getmouse()
            stdscr.addstr(2, 0, f"Mouse: ({x},{y})    ")
            stdscr.refresh()
curses.wrapper(main)
```

#### 4.6.2 手动测试清单（`checklist.md`）

```markdown
# Terminal-Injector 手动测试清单

## cmd.exe
- [ ] 基本输出 echo
- [ ] 颜色 color 命令
- [ ] 清屏 cls
- [ ] 目录列表 dir
- [ ] tree 大量输出
- [ ] 历史命令（上下箭头）
- [ ] Tab 补全
- [ ] Ctrl+C 中断 ping -t
- [ ] 标题 title 命令
- [ ] chcp 65001 中文

## powershell.exe
- [ ] 基本输出
- [ ] VT 颜色 Write-Host -ForegroundColor
- [ ] 进度条 Write-Progress
- [ ] Tab 补全
- [ ] Ctrl+C

## python REPL
- [ ] 基本交互
- [ ] print 带颜色
- [ ] input() 阻塞
- [ ] Ctrl+C KeyboardInterrupt

## python curses TUI
- [ ] 全屏渲染
- [ ] 鼠标点击
- [ ] 滚轮
- [ ] q 退出

## opencode
- [ ] 启动 TUI
- [ ] 键盘导航
- [ ] 鼠标点击
- [ ] 流式输出
- [ ] 分屏
- [ ] 退出

## vim
- [ ] 打开文件
- [ ] Alt Buffer 进入/退出
- [ ] 光标移动 hjkl
- [ ] 方向键
- [ ] 鼠标点击定位
- [ ] 滚轮
- [ ] :wq 保存退出
- [ ] Ctrl+C

## less
- [ ] 打开大文件
- [ ] 滚轮滚动
- [ ] 空格翻页
- [ ] q 退出

## 通用
- [ ] WT 窗口 resize 后重绘正确
- [ ] 关闭 WT tab 后目标程序恢复（无崩溃）
- [ ] 原 cmd 窗口不再更新（静默模式）
- [ ] 长时间运行无内存泄漏
```

#### 4.6.3 自动化测试脚本（`run_all.py`）

```python
#!/usr/bin/env python3
"""Terminal-Injector 自动化测试"""
import subprocess, time, sys, os

def start_target(cmd):
    """启动目标程序，返回 (proc, pid)"""
    proc = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return proc, proc.pid

def inject(pid):
    """注入并启动 mediator"""
    # 用 wt 启动 mediator
    subprocess.Popen([
        "wt.exe", "terminal-injector.exe", "--mediator", "--target-pid", str(pid)
    ])
    time.sleep(2)  # 等待握手

def cleanup(pid):
    """清理：杀目标进程"""
    subprocess.run(["taskkill", "/PID", str(pid), "/F"])

def test_cmd():
    proc, pid = start_target(["cmd.exe", "/k", "echo test"])
    inject(pid)
    time.sleep(5)
    # TODO: 用图像识别或日志验证 WT 输出
    cleanup(pid)

if __name__ == "__main__":
    test_cmd()
    # test_powershell()
    # ...
```

自动化测试难点：验证 WT 渲染结果。方案：
- 截图 + OCR（复杂）
- 检查 mediator 日志确认数据流通
- 人工目视（最终验收）

本 Phase 自动化仅验证"流程跑通无崩溃"，具体渲染正确性靠手动清单。

---

## 5. 最终验收标准

### 5.1 功能完整性

所有 4.6.2 清单项通过。

### 5.2 性能指标

| 指标 | 目标 | 实测 |
|------|------|------|
| 键盘输入延迟 | < 50ms | |
| 鼠标输入延迟 | < 50ms | |
| 满屏输出吞吐 | 60fps | |
| CPU 占用（满载） | < 30% | |
| 内存占用（DLL） | < 20MB | |

### 5.3 稳定性

- 连续运行 1 小时无崩溃
- 反复注入/卸载 10 次无残留
- `windows-debugging` 工具的 umdh 检查无内存泄漏

### 5.4 卸载干净度

- 关闭 WT tab 后目标程序继续运行（不崩溃）
- 原 cmd 窗口恢复可交互
- 任务管理器中 `injected.dll` 不再出现在目标进程模块列表

---

## 6. 风险点

| 风险 | 缓解 |
|------|------|
| `FreeLibraryAndExitThread` 时仍有线程在 Hook 中执行 | `UninstallAll` 已 DisableHook，新调用不再进 Detour；正在执行的会快速返回 |
| 卸载后目标程序调 Console API 崩溃 | Hook 已移除，调真实 API；但 ConsoleState 可能不一致 → 卸载时同步真实状态 |
| 自动化测试无法验证渲染 | 接受人工验收为主；自动化仅验流程 |
| 测试中发现特定程序不兼容 | 记录到已知问题列表，评估是否修复 |

---

## 7. 交付物清单

- [ ] `Unloader` 完整卸载流程
- [ ] DllMain DETACH 最小清理
- [ ] mediator 退出清理
- [ ] `tests/targets/` 测试脚本
- [ ] `tests/manual/checklist.md` 完整清单
- [ ] `tests/runners/run_all.py` 自动化骨架
- [ ] 5.1~5.4 验收全过
- [ ] 项目最终交付
