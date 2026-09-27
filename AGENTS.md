# 总则
- **拒绝采用降级、兼容、缓解方案**
- **未查到根本因素时，不要直接改工程源码**
- bug修复后。**需撤销之前的无效修改点**，不能让无效改动污染代码

# 用户交互
- **必须与用户对齐需求**，防止实现偏差
- 对用户的需求、方案、提出Bug的现象等信息有疑惑，必须及时追问、用户提出方案有不足时，要及时沟通

# 测试
- 请使用`tests\e2e_v2`，`tests\e2e`是旧测试
- 项目的 e2e 测试应该使用 **pywezterm**（`tests\vendor\pywezterm`）进行。若 pywezterm 未下载，请执行`tests\vendor\download_pywezterm.ps1`，因为很多测试依赖它
- pywezterm 文档：`docs\pywezterm\PYWEZTERM_API.md`，若不会使用请尽快查看

# 故障与调试

## 调试方法（仅供参考）

## 日志记录
- bug 的调试过程应该写入`docs\report`
- bug 的调试结果应该写入`docs\working\BUGS.md`
- 修复一个新 bug 之后，应该保证之前的 bug 不会回归