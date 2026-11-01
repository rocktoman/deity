# Deity

**A model-agnostic, reliable and verifiable AI Coding Agent Harness.**

> The model thinks. Deity executes, controls, and verifies.

Deity 是计划以 Python 3.12+ 构建的开源 AI Coding Agent 运行系统。首版以**本地 CLI** 为入口，提供自研 Agent Loop、统一模型适配、工具调度、权限控制、事件记录和可验证的代码任务闭环。

## 当前状态

**V0.3 可靠运行时已形成候选实现。** 在 V0.1 闭环上增加 SQLite v2 事实与 Checkpoint、确定性上下文压缩、租约与显式续跑、模型有界重试和取消、R01–R06 故障注入评估。候选验收结果与当前限制见 [V0.3 验收记录](docs/04-evaluation/v0.3-candidate-2026-10-31.md)。

**V0.4 扩展能力已通过候选验收。** 本地 stdio MCP、按需加载 Skills 与显式注册 Hooks 均默认关闭；确定性扩展评估使用 `uv run --locked python -m evals.runners.extensions`。结果见 [V0.4 验收记录](docs/04-evaluation/v0.4-candidate-2026-10-31.md)，配置和安全边界见 [V0.4 使用说明](docs/03-development/v0.4-usage.md)。

**V0.5 高级编码工作流已形成本地候选实现。** 新增只读 Plan 与显式 Build、三语言 lexical Code Intelligence、Git/补丁保护、确定性 Verification 和 SQLite v4 审计事实；所有能力默认关闭。当前本地门禁见 [V0.5 候选验收记录](docs/04-evaluation/v0.5-candidate-2026-11-01.md)，使用与回退见 [V0.5 使用说明](docs/03-development/v0.5-usage.md)。远程 CI 与真实模型 L3 结果单独记录，不由本地结果替代。

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
| [ADR-011](docs/02-architecture/adr-011-v0.5-advanced-coding.md) | V0.5 Plan/Build、Git Guard 与 Verification 边界 |
| [开发指南](docs/03-development/development-guide.md) | Python 工程初始化、规范、开发顺序 |
| [M0–M5 开发实施文档](docs/03-development/m0-m5-implementation-plan.md) | 各阶段编码、单元/集成测试与验收门禁 |
| [V0.3 Reliable Runtime 实施文档](docs/03-development/v0.3-reliable-runtime-implementation.md) | 压缩、Checkpoint、恢复、重试、取消与长任务门禁 |
| [V0.4 扩展实施文档](docs/03-development/v0.4-extensible-harness-implementation.md) | MCP、Skills、Hooks 与 X01–X06 门禁 |
| [V0.4 使用说明](docs/03-development/v0.4-usage.md) | 扩展配置、CLI、授权和回退 |
| [V0.5 实施计划](docs/03-development/v0.5-implementation-plan.md) | Plan/Build、Code Intelligence、Git 保护与 Verification Engine |
| [V0.5 使用说明](docs/03-development/v0.5-usage.md) | 配置、CLI、安全语义、迁移和回退 |
| [V0.5 候选验收记录](docs/04-evaluation/v0.5-candidate-2026-11-01.md) | 本地测试、A01–A12 与未运行项 |
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
deity mcp list --config ~/.config/deity/config.toml
deity mcp test --config ~/.config/deity/config.toml
deity skills list --workspace /path/to/repo
deity skills inspect <name> --workspace /path/to/repo
deity plan 分析并规划这次修改 --workspace /path/to/repo
deity plans list
deity plans show <plan-id>
deity build --plan <plan-id> --workspace /path/to/repo
deity code symbol <name> --workspace /path/to/repo
deity code references <name> --workspace /path/to/repo
deity verify --show-plan --workspace /path/to/repo
deity verify --run <run-id> --workspace /path/to/repo
```

进入 `deity` 的 `Deity> ` 提示符后，可输入 `/help` 查看交互命令。启用 Skills 后，`/skill` 显示当前工作区的用户与项目 Skill；用 ↑↓ 和回车选择后，**下一条任务**会提示模型加载该 Skill，实际是否加载以工具调用为准。`/skill <name>` 可直接选择。`/mcp` 先选择已配置的 server，再选择其工具；发现阶段不调用业务工具。配置中显式启用的 MCP server 会直接启动，其工具调用不再逐次审批。选中工具只会向模型附加下一条任务的使用提示，不限制运行时可用的其他已配置工具。Esc 返回，非终端输入可用编号选择。斜杠命令不会发给模型；任务运行失败后，选择也不会自动沿用。

独立命令可在仓库目录用 `uv tool install --editable .` 安装。交互模式在一次启动内复用会话，并将最近四轮问答作为后续模型上下文；单次命令执行后退出。模型配置默认从 `~/.config/deity/config.toml` 读取，密钥只从配置指定的环境变量读取。工具权限可显式配置为 `shell = "auto"`、`workspace_write = "auto"`，免去逐次确认；Shell 自动模式仅运行受限的测试和检查命令。删除文件时可以说 `删除 bubble_sort.py`，也可以在同一会话创建文件后说 `删除刚才新建的文件`；模型负责解析目标，受限的 `delete_file` 工具执行删除。

交互模式可输入 `/plan` 进入持续的计划模式：之后的任务会先展示计划并询问一次是否构建。拒绝时可用 `/build` 选择本会话草案，或用 `/build <plan-id> --allow-dirty <path>` 逐路径授权计划内已有改动；`/normal` 返回普通模式。Build 完成后仍保持计划模式。

## MVP 成功标准

在隔离工作区中完成真实的读取 → 搜索 → 修改 → 执行测试 → 汇报证据的闭环；能够正确拒绝越界/危险操作、取消长命令、处理不完整工具参数、保留会话和事件，并通过固定评估集的阻断门禁。

确定性评估命令：`uv run python -m evals.runners.runner` 和 `uv run python -m evals.runners.reliability`。真实模型候选验收单独运行 `uv run python -m evals.runners.l3_candidate`，固定 E01–E03 与长任务，报告写入 `evals/reports/`；密钥只从 `DEEPSEEK_API_KEY` 读取，真实模型结果不代替确定性测试。

V0.4 扩展评估命令：`uv run --locked python -m evals.runners.extensions`。X01–X06 使用本地 fixture、临时 SQLite 和独立子进程，输出写入 `evals/reports/`。

V0.5 高级编码评估命令：`uv run --locked python -m evals.runners.advanced`。A01–A12 覆盖只读 Plan、Plan→Build、漂移、三语言定位、dirty 文件保护、安全补丁、非 Git、验证状态与兼容性。

## 参考与原创边界

学习 Deep Agents、OpenCode、Codex、Pi 的架构与测试思想，但不复制其核心 Runtime。项目开发前应核实第三方许可证与兼容性；仓库级 `AGENTS.md` 是 Deity 自主编写的工程约束。
