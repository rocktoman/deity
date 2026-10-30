# Deity — Harness Engineering 驱动的 AI Coding Agent

> **架构与 MVP 实施计划 V0.1（修订定稿）**
> 状态：技术方向已确认；具体接口可随 PoC 验证迭代
> 核心理念：**The model thinks. Deity executes, controls, and verifies.**

## 0. 执行摘要与已确认决策

Deity 是一个**模型无关、可扩展、可观测、可恢复**的 AI Coding Agent Runtime。首个产品形态为**本地 CLI Coding Agent**，未来可接入 TUI、IDE、Web 和服务端 API。Deity 的价值不是重复实现模型 SDK、终端组件或检索工具，而是构建可靠、可验证的 Agent Harness。

| 决策项 | V0.1 决策 | 约束 |
|---|---|---|
| 核心语言 | **Python 3.12+** | 类型标注、asyncio、清晰接口 |
| 首发交互 | **本地 CLI** | UI 与核心 Runtime 分离，未来拓展 TUI |
| 服务端 | **V0.1 不启动 FastAPI 服务** | FastAPI 可作为后续 HTTP/SSE API 适配层，不能成为核心依赖 |
| Agent 框架 | **自研 Agent Loop / 状态机 / 工具调度 / 上下文管理** | 不依赖 LangChain、LangGraph、Deep Agents Runtime |
| 参考项目 | **Deep Agents、OpenCode、Codex、Pi** | 学习设计思想、测试方法与工程实践，不复制其核心实现 |
| 模型接入 | **官方 SDK + 自研协议适配层** | 首版 OpenAI-compatible + Anthropic 两类协议 |
| 本地持久化 | **SQLite** | 会话、事件、执行记录；迁移由 schema version 管理 |
| 文件搜索 | **ripgrep + Python 文件工具** | 不引入向量数据库 / RAG |
| 安全执行 | **本地工作区 + 权限审批** | 本地模式不宣称 OS 级沙箱；Docker 后续引入 |
| 扩展 | **预留 MCP、Skills、Hooks** | V0.1 不实现这些能力 |
| 评估 | **固定任务集 + 可执行断言** | 不以模型口头宣称完成作为成功标准 |

### 0.1 产品边界

**V0.1 要实现**：从用户输入开始，模型可以读取和搜索代码、生成安全受控的文件补丁、执行命令、观察测试结果、继续修复并输出带证据的报告。

**V0.1 不实现**：TUI、Web、FastAPI 服务、Multi-Agent、MCP、Skills 插件市场、Docker 隔离、RAG、自动跨模型故障切换、复杂任务规划器、分布式消息队列。

**设计原则**：核心层只依赖抽象协议；入口层可以替换；状态显式；副作用可审计；默认最小权限；验证结果由工具产生。

---

## 1. 产品架构与职责边界

```text
CLI (Typer + Rich)
      │  commands / streaming renderer
      ▼
Application Layer (SessionService / RunService)
      │
      ▼
┌────────────────────────────── Deity Core ────────────────────────────────┐
│ Agent Runtime: Agent Loop / Run State / Budgets / Cancellation          │
│ Context Engine: message assembly / token budget / truncation policy     │
│ Event Contract: typed domain events / observer interface                │
└───────────────┬───────────────────────────────────┬──────────────────────┘
                │                                   │
                ▼                                   ▼
       Model Gateway                        Tool Runtime
  Provider / Capabilities              Registry / Dispatcher
  Request & Event Normalize            Schema / Approval / Timeout
                │                                   │
        Official Model SDKs                     Workspace
                                            Files / rg / shell / git
                └─────────────────┬─────────────────┘
                                  ▼
                          Infrastructure
                  SQLite / Logs / Process Runner

Future adapters: TUI / FastAPI HTTP+SSE / IDE → Application Layer
Future execution: Docker sandbox → Tool Executor interface
```

### 1.1 模块职责与禁止跨层调用

| 模块 | 负责 | 不负责 |
|---|---|---|
| `interfaces/cli` | 参数、交互、审批提示、事件渲染 | 直接调用模型、执行文件修改 |
| `application` | 创建会话、发起/取消 Run、协调配置 | 具体工具执行逻辑 |
| `core/runtime` | Agent Loop、步骤预算、状态迁移、终止规则 | 绑定 SDK、直接执行 Shell |
| `core/context` | 构造消息、裁剪大输出、预算控制 | 读取整个仓库作为默认上下文 |
| `model` | 请求/响应/流式事件规范化、能力协商 | 改文件、改变任务状态 |
| `tools` | 工具注册、参数校验、授权、调用与结果规范化 | 决定 Agent 下一轮思考内容 |
| `execution` | 工作区路径、进程执行、超时与取消 | 决定是否允许高风险操作 |
| `persistence` | 会话、事件、运行结果持久化 | 业务策略和模型决策 |
| `evaluation` | 运行固定任务、收集断言与指标 | 参与用户正常任务的决策 |

**依赖方向**：`CLI → Application → Core interfaces`；`Model/Tools/Persistence/Execution` 实现 Core 定义的协议。Core 不导入 Typer、Rich、FastAPI、厂商 SDK 或 SQLite 驱动。

### 1.2 FastAPI 能不能用？

**能用，而且适合后续服务化；但它是 Web 框架，不是 Agent Runtime。** V0.1 是本地 CLI，直接调用 Application 层，避免为了 CLI 引入 HTTP server、端口管理和额外部署。后续新增 `interfaces/http`，用 FastAPI 暴露 `POST /sessions`、`POST /runs`、`GET /runs/{id}/events`（SSE）与取消接口。CLI 与 HTTP 共享同一个 Application/Runtime；如果未来需要远程执行，再讨论服务端工作区、身份鉴权和隔离。

---

## 2. V0.1 最小 Agent Loop 设计

### 2.1 核心流程

```text
User input
   ↓
Create Run + persist RunStarted
   ↓
Build Context (system + project instructions + history + tool results)
   ↓
Call Model (streaming)
   ↓
Normalize output ── error ──> classify / bounded retry / fail
   │
   ├─ final answer ──> optional verification evidence ──> Completed
   │
   └─ tool calls
         ↓
      validate schema → permission check → execute with timeout
         ↓
      persist tool result + emit event
         ↓
      append result to context → next model turn
```

循环由**确定性代码**驱动，模型只提供文本与工具调用建议。单个 Run 有最大轮次、Token/费用预算（能获取用量时）、墙钟时间上限；超限要明确停止并解释原因。V0.1 以**串行工具调用**为默认策略，降低写入冲突；不支持的模型能力显式报错，不静默降级。

### 2.2 运行状态与事件

- Run 状态：`CREATED → RUNNING → COMPLETED | FAILED | CANCELLED`；授权期间为 `WAITING_APPROVAL`。V0.1 不承诺跨进程中断点续跑。
- 事件：`run.started`、`model.requested`、`model.delta`、`model.completed`、`tool.requested`、`tool.approval_required`、`tool.started`、`tool.completed`、`tool.failed`、`run.completed`、`run.failed`、`run.cancelled`。
- 每条持久化事件至少包含：`event_id`、`run_id`、`sequence`、`timestamp`、`type`、`payload`（敏感信息脱敏）。
- **SQLite 事件是审计与诊断记录，不等同于 V0.1 完整可重放/恢复机制**。

### 2.3 核心协议草案（Python 伪代码，供实现时细化）

```python
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

@dataclass(frozen=True)
class ModelCapabilities:
    tool_calling: bool
    streaming: bool
    parallel_tool_calls: bool
    max_context_tokens: int | None

class ModelProvider(Protocol):
    def capabilities(self) -> ModelCapabilities: ...
    async def stream(self, request: "ModelRequest") -> AsyncIterator["ModelEvent"]: ...

class AgentTool(Protocol):
    name: str
    async def execute(self, args: dict, ctx: "ToolContext") -> "ToolResult": ...

class RunStore(Protocol):
    async def append_event(self, event: "RunEvent") -> None: ...
    async def save_run(self, run: "RunRecord") -> None: ...
```

建议 `ModelEvent` 采用可区分联合类型：文本增量、工具调用开始/参数增量/结束、usage、finish、error。**工具调用参数只能在组装完成后解析和校验**；不能把未完成的流式 JSON 直接交给工具执行。

### 2.4 伪代码：执行闭环

```python
async def run_agent(request, deps):
    run = await deps.store.create_run(request)
    await deps.events.emit("run.started", run.id)
    for step in range(request.max_steps):
        deps.cancellation.raise_if_cancelled()
        context = await deps.context.build(run)
        response = await deps.model.collect(context, tools=deps.tools.schemas())
        await deps.store.record_model_response(run.id, response)
        if response.final_answer is not None and not response.tool_calls:
            return await deps.finish(run, response.final_answer)
        if not response.tool_calls:
            return await deps.fail(run, "model produced neither final answer nor tool calls")
        for call in response.tool_calls:  # V0.1: sequential
            deps.cancellation.raise_if_cancelled()
            result = await deps.tools.dispatch_with_policy(call, run)
            await deps.store.record_tool_result(run.id, call, result)
    return await deps.fail(run, "step budget exceeded")
```

实际实现还需要 `try/finally` 资源清理、异常分类、超时、事件流、批准/拒绝结果、工具调用 ID 关联，以及对未知副作用的保护。

### 2.5 失败处理策略

| 失败 | V0.1 行为 |
|---|---|
| 模型 429/临时 5xx | 有界指数退避，优先遵循 Retry-After |
| 鉴权失败/无效模型 ID | 立即失败，给出可操作提示 |
| 工具参数 JSON 无效 | 返回结构化工具错误供模型修正 |
| 命令退出码非零 | 作为工具结果返回，不伪装成功 |
| 命令超时 | 终止进程组（按平台实现），记录超时 |
| Ctrl+C | 取消 Run，终止子进程，落库取消事件 |
| 工具执行结果未知 | 不自动重放有副作用的调用 |
| 上下文超限 | 首版截断/摘要可控的大工具输出；仍超限则明确失败 |

---

## 3. 模型接入：协议统一而非能力假装统一

V0.1 至少实现两种**真正不同**的模型协议适配器：

1. `OpenAICompatibleProvider`：支持配置 `base_url`、`api_key`、`model`，先接一个可用厂商验证。
2. `AnthropicProvider`：通过官方 SDK 接入，验证不同工具调用与流式事件语义。

统一 `ModelRequest / ModelEvent / ModelResponse`，但保留厂商扩展字段；能力探测采用**配置声明 + 必要的运行时校验**，不要根据模型名称硬编码全部能力。默认只启用声明支持的工具调用；模型切换由用户手动完成，**不在执行中自动跨厂商重放**。

配置示意：

```yaml
model:
  provider: openai_compatible
  model: your-model-id
  base_url: https://your-provider.example/v1
  api_key_env: DEITY_MODEL_API_KEY
runtime:
  max_steps: 30
  timeout_seconds: 900
  max_tool_output_chars: 16000
permissions:
  mode: workspace_write
```

**密钥只从环境变量或安全配置读取，绝不写入事件日志。** 上述 URL/模型名称仅为占位示例。

---

## 4. Tool Runtime 与权限边界

### 4.1 最小工具集

| 工具 | 行为 | 默认权限 |
|---|---|---|
| `read_file` | 读取工作区内指定文件，支持行数限制 | 允许 |
| `list_files` | 列目录/Glob | 允许 |
| `grep` | 使用 ripgrep 搜索，限制输出量 | 允许 |
| `apply_patch` | 结构化补丁修改文件，校验上下文 | 需工作区写权限 |
| `write_file` | 创建文件，禁止无确认覆盖 | 需工作区写权限 |
| `shell` | 执行命令，返回 exit code/stdout/stderr | 根据命令与策略审批 |
| `git_status` / `git_diff` | 检查改动 | 允许 |

`apply_patch` 优先于大段覆写；写入前检查路径是否仍位于工作区，避免绝对路径、`..`、符号链接越界；写入操作最好支持原子替换。对二进制文件、超大文件和编码错误给出明确提示。

### 4.2 V0.1 安全策略

- 默认 `workspace_write`，**只允许在用户指定的工作区写文件**；读取外部路径默认拒绝或申请审批。
- Shell 的危险性不能仅靠命令字符串正则完全判断：对高风险命令默认要求确认；可配置只读/需审批/拒绝策略。
- 禁止未经授权执行删除、覆盖敏感路径、安装全局依赖、访问凭证文件等操作。
- 展示即将执行的命令、工作目录、风险说明；支持一次性批准或拒绝。
- 为 Shell 设置超时、输出上限、工作目录、环境变量白名单；防止 API Key 进入子进程环境或日志。
- **本地执行不是沙箱**：工作区路径检查不构成强隔离。Docker/OS 沙箱属于后续版本。
- 不自动提交、重置或清理用户已有的 Git 修改；任何潜在破坏操作必须显式确认。

---

## 5. Context Engine、AGENTS.md 与项目指令

### 5.1 V0.1 上下文构成

1. Deity 内置 System Instructions（工具使用规范、诚实报告验证状态、最小修改原则）。
2. 项目 `AGENTS.md`（若存在）；可扩展读取工作区子目录中的适用指令，但需定义优先级与作用域。
3. 会话消息与本轮用户请求。
4. 模型工具调用及对应工具结果。
5. 当前工作区路径、有限 Git 状态摘要。

**不要默认全量读取仓库**。使用 `list_files` / `grep` / `read_file` 按需发现上下文。工具输出截断时必须明确标注截断，避免模型误判。

### 5.2 Deep Agents 的 AGENTS.md 如何使用

用户确认：**以 Deep Agents 项目的 `AGENTS.md` 为参考基础，加入 Deity 自己的约束。** 这里需要明确两个不同用途：

- **Deity 仓库自身的 `AGENTS.md`**：指导贡献者和 AI 编码工具开发 Deity，包括模块边界、测试命令、代码规范、禁止使用 LangChain/LangGraph 等框架、权限与安全规范。
- **Deity Runtime 对用户项目 `AGENTS.md` 的读取能力**：作为项目级指令注入模型上下文，不等同于直接执行其中的命令。

落地步骤：核对 Deep Agents 官方仓库当前 `AGENTS.md` 的实际内容、适用范围和许可；提取通用工程实践；保留来源与许可要求；重写为适合 Python 自研 Harness 的 Deity 规则。**不要原样照搬其中依赖 LangChain/LangGraph 的运行命令、目录结构或内部约束**。在未逐条审阅原文前，不把任何具体条款宣称为来自 Deep Agents。

建议 Deity `AGENTS.md` 增补的项目特有条款：

- Core 不依赖 CLI / FastAPI / 模型厂商 SDK。
- 禁止引入 LangChain、LangGraph、Deep Agents 作为运行时依赖。
- 工具必须经过 Schema Validation、Permission Check 和 Event Logging。
- 新增 Provider 必须通过统一适配器契约测试。
- 新增工具必须提供权限、超时、错误和取消测试。
- 不允许伪造测试结果；所有验证状态必须可追溯。
- 修改代码时保留用户已有变更；不得擅自执行破坏性 Git 命令。

---

## 6. 数据模型、事件与可观测性

### 6.1 最小实体

- `Session`：`id`、`workspace`、`created_at`、`updated_at`。
- `Run`：`id`、`session_id`、`status`、`model_provider`、`model_id`、`started_at`、`ended_at`、`error_code`。
- `Message`：`id`、`session_id`、`run_id`、`role`、`content`、`created_at`。
- `RunEvent`：`id`、`run_id`、`sequence`、`type`、`payload_json`、`created_at`。
- `ToolExecution`：`id`、`run_id`、`call_id`、`tool_name`、`args_redacted`、`status`、`exit_code`、`duration_ms`。

首版 SQLite 采用 WAL 模式与迁移脚本；敏感信息在写库前脱敏。事件日志记录调用时间、耗时、错误类型、输出截断标记和 token usage（若厂商提供）。不记录原始 API Key。

### 6.2 运行完成报告

```text
Task: 修复 UserService 空指针异常
Status: COMPLETED / FAILED / CANCELLED
Files changed: ...
Verification: mvn test → PASSED / FAILED / NOT RUN
Model calls: ...   Tool calls: ...   Duration: ...
Known limitations: ...
```

`COMPLETED` 表示 Agent 正常结束，**不必然等于任务已通过验证**；报告必须单独列出证据与未验证项目。

---

## 7. 最小评估体系：从第一天建立回归基线

### 7.1 评估原则

- 使用**固定版本、可重置的仓库 Fixture**，任务描述与隐藏断言分离。
- 优先使用**可执行断言**：单元测试、编译、Git Diff、文件存在性、行为输出。
- 区分 `task_success`、`agent_completed`、`verification_passed`，避免一个布尔值掩盖问题。
- 记录模型名称、配置、代码提交版本、任务版本、耗时、token、工具调用次数与失败原因。
- LLM-as-judge 可以后续辅助解释，但不能代替确定性测试。

### 7.2 V0.1 固定任务集（建议至少 8 项）

| ID | 场景 | 断言 | 主要覆盖 |
|---|---|---|---|
| E01 | 分析 Python 小项目结构 | 正确识别入口与核心模块（人工审阅清单） | 搜索/读取 |
| E02 | Python 函数 Bug 修复 | 隐藏 pytest 通过 | 补丁/验证 |
| E03 | 新增 FastAPI 示例接口（测试项目） | API 测试通过 | 跨文件编辑 |
| E04 | Java Spring Boot 空指针修复 | Maven 测试通过 | 跨语言代码理解 |
| E05 | 模型返回无效工具参数 | 不执行工具并返回结构化错误 | 参数校验 |
| E06 | Shell 命令超时 | 子进程被终止且状态正确 | 超时/取消 |
| E07 | 试图写出工作区 | 拒绝且目标文件不变 | 权限/边界 |
| E08 | 工作区已有未提交修改 | 原有修改保留 | Git 安全 |

另外增加**跨 Provider 契约测试**：同一套工具调用、流式事件、错误映射用例分别跑两个适配器。尽量使用 Mock/录制响应测试协议，少量真实 API 测试作为可选集成测试，避免 CI 依赖付费服务。

### 7.3 指标与通过门槛

**工程硬门槛（V0.1 发布阻断项）**：E05～E08 的安全与可靠性断言必须 100% 通过；协议契约测试、单元测试和静态检查必须通过；不得泄漏密钥。

**能力基线（先测量，不拍脑袋设商业级目标）**：记录 E01～E04 的成功率、每任务成本、模型调用次数、工具错误率和 P50/P95 耗时。首次基线用于后续版本比较；同一任务至少重复运行数次以观察波动。

建议输出 `eval-results.json` 与简要 Markdown 报告；每次 Harness 改动后运行离线测试集，定期运行真实模型端到端测试。

---

## 8. 推荐仓库结构（Python Monorepo）

```text
deity/
├── pyproject.toml
├── README.md
├── AGENTS.md
├── src/deity/
│   ├── interfaces/
│   │   └── cli/             # Typer / Rich
│   ├── application/         # session/run use cases
│   ├── core/
│   │   ├── runtime/         # loop, state, budgets, cancellation
│   │   ├── context/         # message assembly, output trimming
│   │   └── events/          # typed event contracts
│   ├── model/
│   │   ├── contracts.py
│   │   └── providers/       # OpenAI-compatible, Anthropic
│   ├── tools/
│   │   ├── registry.py
│   │   ├── policy.py
│   │   └── builtins/        # file, patch, rg, shell, git
│   ├── execution/           # workspace, process runner
│   ├── persistence/         # SQLite repositories, migrations
│   └── config/
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   └── fixtures/
├── evals/
│   ├── tasks/
│   ├── runner/
│   └── reports/
├── docs/
│   ├── architecture/
│   ├── adr/
│   └── roadmap/
└── examples/
```

**推荐成熟依赖**：Typer（CLI）、Rich（渲染）、Pydantic（配置/Schema）、httpx 与厂商官方 SDK（模型协议）、aiosqlite/SQLite（存储）、pytest/pytest-asyncio（测试）、ruff（格式与静态检查）、ripgrep（代码搜索）。具体依赖版本以初始化时兼容性验证为准。优先采用 `uv` 管理 Python 环境和依赖，但不把 uv 设计成核心 Runtime 依赖。

---

## 9. Deity 应重点打造的三个特色

### 特色一：可靠的 Agent Execution Runtime

强调生命周期、超时/取消、错误分类、审计事件、权限与副作用边界。V0.1 做到**可解释地失败**；V0.2/V0.3 再实现受控恢复和 Checkpoint。

### 特色二：真正统一的模型能力抽象

统一事件与工具调用语义，保留差异，显式协商能力。首版两种协议，后续扩展更多厂商与本地推理服务。模型适配层不与工具层耦合。

### 特色三：可验证的任务执行

每个任务提供实际变更与验证证据：`diff`、测试命令、退出码、日志与未运行项。建立固定 Benchmark，持续对比不同 Harness 策略的效果，而不只比较模型回答质量。

---

## 10. 必须规避的架构风险

1. **过度设计**：首版不引入微服务、Redis、RabbitMQ、Kubernetes、向量库、Multi-Agent。
2. **框架锁定**：不使用 LangChain/LangGraph/Deep Agents 作为运行时，核心协议自行定义。
3. **抽象失真**：不假设所有模型都支持同样的流式工具调用和推理参数。
4. **安全假象**：本地工作区限制不等于真正沙箱；高风险命令必须审批。
5. **无评估闭环**：没有可重复测试，就无法判断 Harness 是否变得更可靠。
6. **错误重试引入副作用**：不对写文件、Shell 等非幂等操作进行无条件重放。
7. **过早追求 UI**：首版 CLI，TUI/FastAPI 是接口适配层，不反向污染核心。

---

## 11. V0.1 功能清单与验收定义

### P0（必须）

- [ ] `deity` 进入交互式 CLI；`deity run "..."` 支持单次任务。
- [ ] 支持配置工作区、模型提供商、模型 ID 与 API Key 环境变量。
- [ ] 两种模型协议，统一流式事件和工具调用协议。
- [ ] 自研 Agent Loop：终止、最大步数、超时、取消。
- [ ] 文件读取、列表、搜索、补丁、写入、Shell、Git 状态/Diff。
- [ ] 参数校验、工作区边界、危险操作审批。
- [ ] SQLite 会话与事件记录；基础日志脱敏。
- [ ] 任务结束报告含实际验证状态。
- [ ] 固定评估任务 E01～E08 与自动化测试脚本。

### P1（可在 V0.1 后的小版本追加）

- [ ] 更完整的 token 预算和成本统计。
- [ ] 工具输出智能截断与摘要。
- [ ] 会话继续（不等同于中断任务恢复）。
- [ ] 更细粒度的审批策略。

### 明确不做

TUI、FastAPI 服务、Docker Sandbox、MCP、Skills Runtime、Sub-Agent、自动模型故障切换、复杂 Checkpoint/Replay、RAG。

### V0.1 Definition of Done

**真实用户闭环**：在干净的 Python/Java 测试仓库中，用户通过 CLI 提出修改任务，Deity 自主读取文件、修改代码、执行指定测试、输出真实结果；Ctrl+C 可中断；拒绝越界操作；日志可追踪；安全测试全部通过。

---

## 12. 分阶段开发计划

| 阶段 | 建议时长 | 交付物 | 验收点 |
|---|---|---|---|
| P0：调研与接口 | 1 周 | ADR、接口契约、最小 SDK 流式 PoC | 两种模型协议可规范化 |
| P1：Agent Loop | 1 周 | Runtime、事件、状态、取消 | 模拟工具调用闭环通过 |
| P2：工具与安全 | 1～2 周 | 文件/rg/shell/git、审批 | E05～E08 通过 |
| P3：CLI 与存储 | 1 周 | CLI、SQLite、执行报告 | 会话事件可查询 |
| P4：端到端评估 | 1～2 周 | E01～E04、真实模型基线 | 实际代码修改+测试闭环 |

**预计 5～7 周完成可演示的 V0.1**（单人兼职开发估算，取决于可投入时间与 SDK 适配难度）。原规划中 V0.2～V1.0 的方向保留：可靠运行时 → 上下文与 Checkpoint → MCP/Skills/Sandbox → 高级 Coding Workflow → Multi-Agent → 产品化。

### 推荐实施顺序（前 10 个工作日）

1. 初始化 Python 包、`uv`、pytest、ruff、CLI 空壳。
2. 定义 `ModelRequest/ModelEvent/ToolCall/ToolResult/RunEvent`。
3. 实现 OpenAI-compatible 流式适配器与 Mock Provider。
4. 实现 Anthropic 适配器，跑统一契约测试。
5. 实现最小 Agent Loop 与终止条件。
6. 实现 `read_file`、`list_files`、`grep`。
7. 实现 `apply_patch` 与写入边界检查。
8. 实现 `shell` 的超时、取消、stdout/stderr/exit code。
9. 接入审批提示与 CLI 流式渲染。
10. 运行一个真实 Bug 修复任务，记录失败点并修正架构。

---

## 13. 关键 ADR（架构决策记录）

**ADR-001：Python 作为核心语言。** 采用 asyncio 和类型化接口；理由是 Python Agent 生态、快速迭代和工具集成能力。代价是对并发任务、子进程与跨平台信号处理需仔细测试。

**ADR-002：CLI-first，FastAPI-later。** V0.1 不运行服务；所有交互走 Application API。未来 FastAPI 只承担 HTTP/SSE、鉴权与请求转换。

**ADR-003：不使用现成 Agent Framework。** 允许官方模型 SDK、Pydantic、Typer、SQLite、ripgrep、MCP SDK 等基础设施；Agent Loop、状态机、工具权限、上下文策略自研。

**ADR-004：本地执行先行。** V0.1 使用受限本地工作区和显式审批；不宣称强沙箱。后续通过 `Executor` 抽象接入 Docker。

**ADR-005：事件记录与验证优先。** 从第一版就保存结构化执行事件与测试证据；Checkpoint、恢复和回放逐步增强。

---

## 14. 后续待决事项（不阻塞 V0.1）

- 多模型能力描述采用静态配置、在线探测还是混合机制的细节。
- TUI 选择 Textual、prompt_toolkit 或其他终端组件。
- FastAPI 的 API 协议、SSE 事件版本及远程鉴权方案。
- Docker Sandbox 的挂载、网络策略与资源限制。
- MCP、Skills、Hooks 的加载顺序与权限继承。
- Checkpoint 是基于 Git 工作树、快照还是事件+状态混合实现。
- 更大规模 Benchmark 与跨模型成本/成功率对比。

---

## 15. 参考项目与借鉴原则

- **Deep Agents**：学习项目级指令、工具组织、上下文与长任务工程经验；不引入其基于 LangChain/LangGraph 的运行时。
- **OpenCode**：学习多模型接入、CLI/TUI 解耦、会话与工具体验。
- **Codex**：学习执行权限、沙箱边界、补丁与验证流程。
- **Pi**：学习最小化 Agent Loop、扩展点和简洁运行时设计。

官方项目入口（仅供后续调研）：

- Deep Agents: https://github.com/langchain-ai/deepagents
- OpenCode: https://github.com/anomalyco/opencode
- Codex: https://github.com/openai/codex
- Pi: https://github.com/badlogic/pi-mono

**实现要求**：参考理念与公开协议，逐项核实源码、许可证及当前版本；不要照抄项目内部代码或假定其实现细节恒定。

---

## 16. 项目宣言

> **Deity — An Extensible, Model-Agnostic AI Coding Agent Harness.**
>
> 模型负责理解、推理与决策；Deity 负责工具、状态、执行、安全和验证。
>
> **不重新发明基础组件，专注打造可靠、可验证、模型无关的 Agent Runtime。**
