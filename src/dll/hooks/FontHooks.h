// 字体类 Console API Hook 声明（Phase 8）
// 详见 docs/phases/08-advanced-features.md 4.4
//
// 职责：
//   - GetCurrentConsoleFontEx → 返回 ConsoleState 缓存（注入瞬间快照）
//   - SetCurrentConsoleFontEx → 仅记录到缓存，不真改（WT 字体由用户配置控制）
//   - GetConsoleFontSize      → 返回缓存中的 dwFontSize
//
// 设计理由：WT 的字体由用户在 settings.json 配置，DLL 无法也无需改变。
//   目标程序调用 SetCurrentConsoleFontEx 改字体时，DLL 假装接受（返回 TRUE）
//   但实际不改变 WT 渲染。GetCurrentConsoleFontEx 始终返回注入瞬间的快照，
//   保证目标程序读到的字体尺寸稳定，避免布局错乱。
#pragma once

namespace terminjector::hooks {

// 注册字体类 Hook（由 DllMain 调用）
void RegisterFontHooks();

} // namespace terminjector::hooks
