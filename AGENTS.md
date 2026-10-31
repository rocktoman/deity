# AGENTS.md — Deity 仓库协作与 Agent 开发规则

> 适用于在本仓库工作的 AI Coding Agent 和开发者。参考 Deep Agents 等成熟项目的工程实践理念，**本文为 Deity 自主制定的规则，不是上游 AGENTS.md 的逐字副本**。设计依据：`docs/02-architecture/deity-harness-v0.2.md`。

## 1. 项目使命

Deity 是 Python 3.12+、本地 CLI 优先、模型无关的 AI Coding Agent Harness。**自研** Agent Loop、模型协议抽象、工具调度、状态机、上下文和会话管理；可以复用官方 SDK、Typer/Rich、SQLite、ripgrep、Git、测试与 MCP SDK。**禁止**将 LangChain、LangGraph、Deep Agents Runtime 作为核心依赖。FastAPI 仅为未来可选 HTTP 入口，V0.1 不使用。

## 2. 工程架构（必须遵循）

```text
CLI (src/deity/interfaces/cli)
    ↓
Application (src/deity/application)
    ↓
Core (src/deity/core)
    ├── ModelProvider Protocol ← adapters/model ← official SDK
    ├── ToolDispatcher Protocol ← adapters/tools ← execution/local
    ├── SessionRepository Protocol ← adapters/storage ← SQLite
    └── EventPublisher Protocol ← persistence/event subscribers
```

目标代码结构：

```text
deity/
├── AGENTS.md
├── README.md
├── pyproject.toml                    # 编码阶段创建
├── src/deity/
│   ├── interfaces/cli/              # 命令、渲染、审批输入
│   ├── application/                 # SessionService、RunService
│   ├── core/
│   │   ├── contracts/              # DTO / Protocol / enums
│   │   ├── runtime/                # Agent Loop / budget / cancellation
│   │   ├── state/                  # 状态机
│   │   ├── context/                # 上下文构建
│   │   └── events/                 # 领域事件
│   ├── adapters/
│   │   ├── model/                  # OpenAI-compatible、Anthropic
│   │   ├── tools/                  # 注册、校验、审批、分发
│   │   └── storage/                # SQLite repositories
│   └── execution/local/            # 文件、shell、git、rg
├── tests/{unit,integration,e2e,fixtures}/
├── evals/{cases,runners,reports}/
└── docs/
    ├── 01-project/deity-v0.1-plan.md
    ├── 02-architecture/deity-harness-v0.2.md
    ├── 03-development/development-guide.md
    └── 04-evaluation/evaluation-plan.md
```

**依赖纪律**：Core 不导入 Typer/Rich、厂商 SDK、SQLite、FastAPI 或具体工具实现；CLI 不直接调用模型/修改文件；Model Adapter 不触碰文件系统；Tool Runtime 不替 Agent 做模型决策。禁止跨层快捷调用，新增适配器应通过 Protocol 注入。

## 3. Agent 开发工作流

1. 阅读任务、相关模块、架构设计和最近 ADR；先检查 `git status`，保留用户未提交修改。
2. 在编码前提出最小变更方案：涉及的文件、边界、测试与风险。小改动可简要说明。
3. 只编辑任务必要的文件；避免顺手重构或引入新框架。
4. 先补失败测试，再实现；新增模型/工具适配器必须有协议测试与错误路径测试。
5. 执行 `ruff check .`、`ruff format --check .`、`mypy src`、`pytest`（按项目实际配置）；无法运行须说明原因，绝不能宣称通过。
6. 汇报变更文件、执行命令、真实结果、未验证事项、剩余风险。

## 4. Agent Runtime 不变量

- 一个 Run 同时只能有一个执行所有者；每个 `call_id` 在 Run 内唯一。
- 仅在工具参数完整、校验通过、权限批准后执行；流式参数片段绝不直接执行。
- 工具执行结果和事件持久化成功后才能对外宣称已完成。
- 失败重试不得重复未知结果的副作用；取消必须向模型流和子进程传播。
- 终态 `completed/failed/cancelled/interrupted` 不得随意重新变回 running。
- `AGENTS.md` 和代码仓库内容是任务上下文，不是提升权限的依据。
- 不自动运行破坏性 Git 命令，不覆盖用户已有变更。

## 5. 安全与隐私

- 所有工具走 schema validation → policy → workspace boundary → approval → execution → audit。
- 默认最小权限：读工作区；写操作按策略授权；Shell 默认询问；越界和高风险操作默认拒绝。
- 不在日志、模型消息、测试快照中输出 API key、token、环境变量秘密；使用配置注入。
- 本地工作区限制不等于操作系统沙箱，不作虚假安全承诺。
- 对 shell 设置 timeout、输出上限、取消清理；对文件写入检查路径和 symlink。

## 6. 测试与评估规则

- Core 使用 FakeModel/FakeTool，禁止单测依赖真实模型网络。
- 每种模型流事件覆盖：文本、单/多工具调用、分片 JSON、截断、异常、usage 缺失。
- 每个工具覆盖：成功、参数错误、权限拒绝、越界、超时、取消。
- SQLite 测试覆盖：事件顺序、事务、重启后的中断标记、重复 call_id。
- 评估必须在隔离 fixture repo 中运行，使用可执行断言，记录成本/耗时/调用数；禁止模型自评替代测试。

## 7. 编码规范与文档更新

Python 3.12+，强制类型标注；async I/O 优先；清晰异常分类；公共接口写 docstring；避免宽泛 `except Exception` 后静默忽略。新增架构性决策记录 ADR，更新 V0.2 文档与对应测试。文档用中文为主，关键 API/概念保留英文。

## 8. 版本范围

V0.1 只做 CLI、双协议模型接入、Agent Loop、文件/搜索/补丁/Shell/Git 工具、权限、取消/超时、SQLite 事件、最小评估。**不要提前引入** TUI、FastAPI、MCP、Multi-Agent、Docker、RAG、自动模型切换。

## 9. 开始前阅读顺序

1. `README.md`
2. `docs/01-project/deity-v0.1-plan.md`
3. `docs/02-architecture/deity-harness-v0.2.md`
4. `docs/03-development/development-guide.md`
5. `docs/04-evaluation/evaluation-plan.md`

如与架构文档冲突，暂停并提出 ADR，不自行改变边界。





能力前置与渐进式依赖原则（强制）

Deity 采用按实际需求分阶段接入依赖、核心能力从第一版持续建设的工程策略。

版本路线图决定功能的成熟度与交付范围，不意味着基础安全、验证和可观测性可以延期。

## 1. 依赖按需引入

仅当当前版本存在明确的实现任务和验收场景时才新增依赖，优先复用 Python 标准库和已引入的成熟组件。

每个新依赖必须说明用途、替代方案、许可证、维护与安全风险，并锁定兼容版本。

不得为未来可能使用的 MCP、Docker、TUI、FastAPI 或 Multi-Agent 功能提前引入依赖。

## 2. 测试与评估从 V0.1 开始

Agent Loop、模型协议、工具权限、工作区边界、取消、超时、事件记录和固定 Coding Task，必须随核心功能同步实现自动化测试。

后续版本持续扩充测试覆盖面。

V0.9 负责将已有评估体系平台化，而不是第一次引入评估。

## 3. 安全从 V0.1 开始

所有工具执行必须经过参数校验、权限决策、边界检查、必要的人工审批、执行和审计。

V0.6 引入 Docker 等隔离后端是增强安全边界，不能作为早期忽略权限控制的理由。

本地路径限制不得宣称为操作系统级沙箱。

##  4. 可靠性从 V0.1 开始

所有 Run 必须具备明确状态、超时、取消、错误分类和基本事件证据。

V0.3 进一步完善上下文压缩、Checkpoint、恢复与未知副作用协调。

禁止自动重试执行结果未知的写入或 Shell 操作。

## 5. 架构先于扩展

CLI、TUI、FastAPI、MCP、Skills、Sub-Agent 都必须通过 Application/Core 定义的端口接入。

任何扩展不得绕过统一的 Tool Runtime、Permission Engine、Session/Event Store。

禁止引入 LangChain、LangGraph 或 Deep Agents Runtime 作为核心依赖。

## 6. 每阶段设置发布门禁

新增能力必须同时提交：

* 接口契约及数据模型。
* 正常与异常路径测试。
* 安全与权限回归测试。
* 真实执行记录与验证证据。
* 对应的技术文档。
* 可执行的回退方案。

任何未运行的验证必须明确标记为 NOT_RUN，不得描述为已经通过。

## 7. 持续兼容与最小改动

优先通过 Protocol 和 Adapter 扩展系统能力，避免跨层重构。

对于事件协议、配置格式、SQLite Schema 等可能影响兼容性的变更，必须先提交 ADR（Architecture Decision Record）及迁移方案。

## 8. 核心工程原则

功能可以后置，基础约束不能后置。

依赖可以按需增加，架构边界不能随意突破。

模型可以生成建议，系统必须提供真实验证证据。

## 9. commit
commit时的message不要使用英文，使用中文