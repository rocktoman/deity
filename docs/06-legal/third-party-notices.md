# 第三方来源声明

Deity 的实现均为自研；个别模块在设计与算法层面参照了开源项目，
按其许可证要求在此声明来源。参照方式为阅读原文后用 Python
重新实现，不复制源代码文本。

## openai/codex（Apache License 2.0）

- 参照对象：`codex-rs/apply-patch` crate（提交 ee344ca）：
  - `src/parser.rs` 的 patch 文法（`*** Begin Patch` 等标记、
    Add/Delete/Update/Move hunk、`@@` 上下文锚、`*** End of File`）；
  - `src/seek_sequence.rs` 的四级模糊定位（精确 → 忽略行尾空白 →
    忽略首尾空白 → Unicode 标点归一）；
  - `src/file_update.rs` 的「先全量校验、后落盘」应用语义。
- 本仓库对应实现：`src/deity/adapters/tools/patch.py`（ADR-018）。
  一处刻意差异：old_lines 为空的 chunk（纯插入）在锚点位置插入，
  而非追加到文件末尾。
- 许可证全文：https://www.apache.org/licenses/LICENSE-2.0
  （Apache License, Version 2.0；Copyright OpenAI）。
