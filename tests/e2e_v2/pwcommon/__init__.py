"""e2e_v2 公共库（purposely not named `common`/`helpers`）。

命名理由：v1 的 `tests/e2e/keyboard/` 曾被同名第三方库顶掉，9 个用例在导入期
就死（见 docs/PHASES.md BUG-017）。v2 与 v1 并存期间，双方都不该占用
`common` / `helpers` 这类通用包名。
"""
