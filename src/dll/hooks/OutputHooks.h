// 输出类 Hook 声明
// 详见 docs/phases/03-dll-framework.md 4.5.2
//
// Phase 3 仅注册 WriteConsoleW/A
// Phase 4+ 扩展 FillConsoleOutputCharacter / ScrollConsoleScreenBuffer 等
#pragma once

namespace terminjector::hooks {

// 注册所有输出类 Hook（向 HookManager 登记.detour 与 original）
// 由 DllMain 在 InstallAll 之前调用
void RegisterOutputHooks();

} // namespace terminjector::hooks
