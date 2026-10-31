# Deity

**A model-agnostic, reliable and verifiable AI Coding Agent Harness.**

> The model thinks. Deity executes, controls, and verifies.

Deity 是计划以 Python 3.12+ 构建的开源 AI Coding Agent 运行系统。首版以**本地 CLI** 为入口，提供自研 Agent Loop、统一模型适配、工具调度、权限控制、事件记录和可验证的代码任务闭环。

## 当前状态

**V0.3 可靠运行时已形成候选实现。** 在 V0.1 闭环上增加 SQLite v2 事实与 Checkpoint、确定性上下文压缩、租约与显式续跑、模型有界重试和取消、R01–R06 故障注入评估。候选验收结果与当前限制见 [V0.3 验收记录](docs/04-evaluation/v0.3-candidate-2026-10-31.md)。

## 核心选择

- Python 3.12+；本地 CLI 优先，未来可扩展 TUI、FastAPI、IDE。
- 自研 Harness 核心，不使用 LangChain、LangGraph 或 Deep Agents Runtime。
- 第一阶段接入 OpenAI-compatible 与 Anthropic 两类协议；模型 SDK 复用成熟实现。
- 工具：读取/搜索/修改文件、Shell、Git；默认最小权限。
- SQLite 保存会话、运行状态、工具结果与事件；测试/验证结果来自实际执行。

## 架构概览

```text
CLI → Application → Agent Runtime
                    ├── Model Gateway → Provider SDK
                    ├── Context Engine
                    ├── Tool Runtime → Permission → Local Execution
                    └── Session / Event Store → SQLite
```

## 文档导航

| 文档 | 内容 |
|---|---|
| [V0.1 项目规划](docs/01-project/deity-v0.1-plan.md) | 定位、范围、选型、路线图 |
| [V0.2 核心架构](docs/02-architecture/deity-harness-v0.2.md) | 模块接口、状态机、模型/工具协议、SQLite、时序 |
| [ADR-006](docs/02-architecture/adr-006-aiosqlite-v0.1.md) | SQLite 异步存储选择及限制 |
| [ADR-007](docs/02-architecture/adr-007-v0.3-reliable-resume.md) | V0.3 安全恢复与副作用边界 |
| [开发指南](docs/03-development/development-guide.md) | Python 工程初始化、规范、开发顺序 |
| [M0–M5 开发实施文档](docs/03-development/m0-m5-implementation-plan.md) | 各阶段编码、单元/集成测试与验收门禁 |
| [V0.3 Reliable Runtime 实施文档](docs/03-development/v0.3-reliable-runtime-implementation.md) | 压缩、Checkpoint、恢复、重试、取消与长任务门禁 |
| [评估计划](docs/04-evaluation/evaluation-plan.md) | 固定测试任务、指标、门禁 |
| [DeepSeek L3 基线](docs/04-evaluation/l3-deepseek-v4-flash-2026-10-31.md) | 真实模型 E01–E03 验收结果与限制 |
| [AGENTS.md](AGENTS.md) | 仓库工程架构和 AI 协作规则 |

## CLI

```bash
deity --help
deity                             # 进入交互模式；每次直接输入问题，/exit 退出
deity run                         # 同样可进入交互模式，兼容旧用法
deity run 分析这个 Python 项目的架构    # 单次任务；可不加引号
deity sessions
deity runs inspect <run-id> --workspace /path/to/repo
deity resume <run-id> --workspace /path/to/repo --config ~/.config/deity/config.toml
```

独立命令可在仓库目录用 `uv tool install --editable .` 安装。交互模式在一次启动内复用会话，并将最近四轮问答作为后续模型上下文；单次命令执行后退出。模型配置默认从 `~/.config/deity/config.toml` 读取，密钥只从配置指定的环境变量读取。

## MVP 成功标准

在隔离工作区中完成真实的读取 → 搜索 → 修改 → 执行测试 → 汇报证据的闭环；能够正确拒绝越界/危险操作、取消长命令、处理不完整工具参数、保留会话和事件，并通过固定评估集的阻断门禁。

确定性评估命令：`uv run python -m evals.runners.runner` 和 `uv run python -m evals.runners.reliability`。真实模型候选验收单独运行 `uv run python -m evals.runners.l3_candidate`，固定 E01–E03 与长任务，报告写入 `evals/reports/`；密钥只从 `DEEPSEEK_API_KEY` 读取，真实模型结果不代替确定性测试。

## 参考与原创边界

学习 Deep Agents、OpenCode、Codex、Pi 的架构与测试思想，但不复制其核心 Runtime。项目开发前应核实第三方许可证与兼容性；仓库级 `AGENTS.md` 是 Deity 自主编写的工程约束。
