# 22. ConHost 卸载重放与 prompt 截断（PromptTracker）

> 对应实现：`src/dll/state/PromptTracker.h/.cpp`、`src/dll/Unloader.cpp`（ReplaySessionToConHost）

## 1. 背景

注入时 ConHost 缓冲被冻结（会话期输出被拦截路由到 WT，写不进 ConHost 缓冲）。
卸载时 `ReplaySessionToConHost` 把会话期增量 VT 流（`VtReplayBuffer`）重放到
ConHost 冻结缓冲之上，恢复会话末画面。

**双 prompt 累积问题**：会话末画面以 prompt 结尾；若重放把该 prompt 也写进
ConHost，cmd 被 KickStart 回车唤醒后又自绘一个新 prompt → 每次注入/卸载循环
多出一行 prompt。旧启发式"重放后读回末行、以 `>` 结尾则擦除"需猜内容，
`echo x > file` 之类会误擦。

## 2. 写-读序列语义（PromptTracker）

行编辑 shell 的循环必然是"**画 prompt → 阻塞 ReadConsole**"：
"行编辑读入口之前的最后一次内容写入"就是该读的 prompt —— 不需要猜内容。

- 写入侧：`BatchSender::EnqueueVtOutput` 每次追加重放缓冲后把起始偏移记入
  `thread_local tls_lastWriteOffset`（thread_local：prompt 绘制与后续读同线程，
  避免其他线程背景输出污染）。
- 读侧：`ReadConsoleW/A` detour 行编辑段（ECHO_INPUT 生效时）进入/离开分别
  `OnLineReadBegin`（快照 `m_lastPromptOffset = tls_lastWriteOffset`，读计数++）/
  `OnLineReadEnd`（读计数--）。
- 卸载门（`TruncateOffset`）：**读计数 > 0**（shell 正停在 prompt 等输入）
  且已记录 prompt 候选 → 返回重放截断偏移，否则 `nullopt`（全量重放）。
  - 命令执行中卸载（长输出如 `tree /f`）→ 无行编辑读阻塞 → 不截断，不丢输出。
  - TUI 全屏程序（无 ECHO_INPUT）→ 不记录 → 永不截断。

## 3. ReplaySessionToConHost 重放流程

1. 缓冲只放大不缩小（保留冻结缓冲的 scrollback 历史）。
2. 视口顶部保持注入时 `srWindow.Top`（重放 CUP 是视口相对坐标，行号基准 =
   注入时窗口顶部），尺寸用会话尺寸。
3. 光标归位：重放前光标在窗口外时移到 `(0, 注入光标行)`；并记录重放前光标
   （供惰性重放判定）。
4. **4.0 截断决策**：`TruncateOffset()` 命中 → `replayEnd = prompt 写入起点`。
5. **4.1 擦除快照 prompt 行**（截断命中时，重放前）：
   `FillConsoleOutputCharacterW` + `FillConsoleOutputAttribute` 擦掉
   `(0, 注入光标行)` 整行。
6. **4.2 分块重放** `vt[0, replayEnd)`（WriteFile 字节流，ConHost VT 模式解析；
   重放前输出代码页切 UTF-8，结束后恢复）。
7. **5.5 重放后光标**：
   - 未截断：光标推进到下一行行首（cmd 新 prompt 落新行，避免拼到输出末行）。
   - 已截断 + 惰性重放（重放前后光标未动 = 空会话无可视内容）：光标抬到
     擦除行上一行（`(0, injCur.Y - 1)`，下限 0）。

## 4. 实测教训（2026-08-08）

截断机制初版验证暴露两个设计误判，均已在第 3 节实现中修正：

1. **快照旧 prompt 行不会被重放覆盖**。截断后重放内容 = `vt[0, prompt起点)`，
   prompt 文本（及其 SGR 前缀）被排除；重放只写当前光标处。实测空会话
   重放仅 `ESC[0m`（4 字节）落在 (0,4)，快照 prompt 行 (0,3) 纹丝不动 →
   必须有 4.1 的确定性擦除。初版曾误以为"截断点 > 0 时重放已覆盖该行"。

2. **cmd 自绘的新 prompt 不落在原位**。KickStart 回车以 ECHO_INPUT 被
   ReadConsoleW 消费，ConHost 回显 `\r\n` 把光标推到下一行，cmd 新 prompt
   落在旧行下一行 → 空会话画面 = 旧 prompt（已擦除变空行）+ 空行 + 新 prompt，
   多出一行。惰性重放时把光标抬到擦除行上一行，回显 `\r\n` 恰好把新 prompt
   推回注入前原位 → 画面与注入前逐像素一致。

## 5. 验证

| 测试 | 结果 |
|---|---|
| 逐像素一致性（一次性脚本，无输入 3 轮，注入→卸载；已清理） | 3/3：每轮卸载后 nz=[0,1,3]、prompt@3、光标 (41,3)，与注入前逐像素一致 |
| `tests/e2e/lifecycle/test_repeat_inject_unload.py`（现存，含单 prompt 断言） | 10/10：prompt 行数恒为 1，无进程/WT 残留 |

## 6. 残余边界

- 命令执行中卸载：不截断，全量重放，保留全部输出。
- 批处理 `set /p` 等非重绘 prompt：截断会少该 prompt 行（该读已被 KickStart
  回车消费，prompt 文本不会重绘）——语义正确、视觉损失极小。
- 重放缓冲达 4MB 上限：`Append` 返回 -1 不记录，退化为全量重放
  （旧双 prompt 风险仅在该资源上限场景）。
