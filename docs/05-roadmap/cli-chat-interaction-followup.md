# CLI 聊天对话交互优化（后续项，v1.0 之后）

> 状态：已记录，未启动。触发条件：v0.8、v0.9、v1.0 全部开发完成、全量功能测试跑完并汇报后，再启动。不要插队到 v0.8–v1.0 之前，不要现在改 CLI。

## 背景

用户文喜要求：参考 Claude Code，优化重构 CLI 聊天对话交互。

## 现状（2026-11-09 已确认）

- `src/deity/interfaces/cli/main.py` 的 `_interactive()` 是 `input()` 循环，提示符 `Deity>` / `Deity[plan]>`。
- 仅有 `/plan` `/build` `/normal` `/skill` `/mcp` `/help` `/exit`（`/quit`）等命令。
- 模型结果要等整轮跑完才一次性 `console.print`，没有流式显示。
- 没有历史回溯、没有多行输入、没有 `@文件引用`、没有 `/命令补全`。
- 审批只是 `typer.confirm` 打印工具参数，没有 diff 展示。
- `src/deity/interfaces/cli/menu.py` 是手写 raw tty 上下键选择器，只用于 `/skill` `/mcp` `/build`。

## 目标（启动时再细化方案）

参考 Claude Code 的交互体验，补齐流式显示、历史回溯、多行输入、`@文件引用`、`/命令补全`、带 diff 的审批等能力。

## 约束（启动时必须遵守）

- 按 `AGENTS.md` 边界：CLI 只做渲染与输入，不直接调模型、不直接改文件；通过 Application/Core 端口接入。
- 不把 Textual 变成基础 CLI 的硬依赖（v0.8 的 Textual 仅为可选 extra；基础 CLI 保持轻依赖，见 v0.8 设计）。
- 先补失败测试再实现；质量门禁 `ruff check`、`ruff format --check`、`mypy src`、`pytest`。
- commit message 用中文；不在日志/测试快照中输出密钥。

## 与 v0.8–v1.0 的关系

- v0.8 的统一客户端协议（`start_run` / `subscribe_events` / `approve` / `cancel` / `resume`）与事件流是本项可复用的基础，但本项不属于 v0.8 验收范围。
- 本项启动前，先确认 v0.8 TUI 的事件订阅与审批 API 是否已稳定，再决定 CLI 交互是否复用同一协议。
