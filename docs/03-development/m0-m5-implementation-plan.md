# Deity V0.1 开发实施文档：M0–M5

> 状态：实施中。本文是 V0.1 开发与验收任务清单；具体完成情况以测试结果和评估报告为准，不能将未运行的 L3 真实模型基线视为通过。

## 1. 当前基线与执行原则

当前仓库只有 `AGENTS.md`、`README.md` 和四份项目文档；没有 `pyproject.toml`、`src/`、`tests/`、`evals/`。当前目录没有 `.git`，所以尚不能执行基于仓库的 Git 检查或 CI 验收。开始编码前先确认最终工作区及版本控制方式，保留任何已有文件和本地改动。

严格按 **M0 → M1 → M2 → M3 → M4 → M5** 推进。每个功能先写会失败的测试，再写最小实现，最后运行本阶段及既有回归。只有本阶段的代码、单元测试、集成测试和可复现的验收证据都具备，才进入下一阶段。所有真实模型请求和有副作用的 E2E 均在隔离 fixture 工作区、明确配置的预算下执行。

依赖方向固定为 `CLI → Application → Core Protocol`，模型、工具和 SQLite 由适配器注入；Core 不导入 UI、SDK 或 SQLite。V0.1 不引入 FastAPI、TUI、MCP、Multi-Agent、Docker、RAG 或自动跨模型切换。涉及分层、事件协议或权限默认值的变动，先写 ADR 并同步架构文档。

## 2. 所有里程碑共用的质量门禁

每阶段完成时运行下列命令；M0 创建工具配置后这些命令才应可用。失败必须修复或记录为阻塞，不得把“未运行”写成“通过”。

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q tests/unit
uv run pytest -q tests/integration
```

单元测试只用 FakeModel/FakeTool，不联网；集成测试用临时工作区、临时 SQLite 和可控进程。测试报告至少记录命令、退出码、失败摘要、运行环境、未运行项。工具/模型测试均覆盖成功和错误路径。最终报告中的验证状态使用 `PASS/FAIL/NOT_RUN/UNKNOWN`，仅以实际工具结果和断言为证据。

## 3. M0 — Python 工程初始化

**目标**：获得可安装、可调用、可测试的最小 Python 3.12+ 包，不提前实现 Agent 行为。

**编码交付**

1. 创建 `pyproject.toml`、`src/deity/__init__.py`、`src/deity/interfaces/cli/main.py`、`tests/unit/`、`tests/integration/`；配置 `deity` 脚本入口、Python 版本、pytest/ruff/mypy。
2. 使用 `uv` 管理依赖和锁文件。运行依赖按使用阶段引入：M0 先放 CLI 与配置所需包；OpenAI/Anthropic SDK 在 M2、SQLite 异步库在 M4 确定实现方案时再引入。
3. CLI 只提供 `--help`、`--version` 和清楚的“功能未实现”反馈；配置解析在入口或 Application 组合层，不允许 CLI 直接触碰模型或文件写入。
4. 增加本地质量检查入口；若建立 CI，先运行 lint、format、typecheck、unit、integration。初始化 Git 仓库属于单独工作区决策，不把当前目录假定成已初始化仓库。

**先写测试**：CLI 帮助与版本输出、无效配置返回非零退出码且不泄露密钥。**集成测试**：从安装后的脚本入口启动 CLI，验证帮助、退出码与配置加载。**退出门禁**：包能安装；`uv run deity --help` 可执行；本节质量命令全通过。

## 4. M1 — Deity Core

**目标**：仅用假模型和假工具跑通确定性 Agent Loop，为后续适配器提供稳定契约。

**编码交付**

1. 在 `src/deity/core/contracts/` 定义 `AgentRequest/AgentResult`、`ToolCall/ToolResult`、`ModelRequest/ModelEvent`、能力、错误与 `ModelProvider/ToolDispatcher/SessionRepository/EventPublisher` Protocol。稳定 ID、`call_id` 与状态语义遵循 V0.2 架构。
2. 在 `core/state/` 实现 `CREATED/RUNNING/WAITING_APPROVAL/COMPLETED/FAILED/CANCELLED` 的显式迁移；异常退出后的 `interrupted` 留作 M4 存储恢复终态。终态不能重新运行。
3. 在 `core/runtime/` 实现单 Run 单执行所有者、模型流聚合、完整 JSON 后提交工具调用、同一响应多工具串行执行、唯一 `call_id`、轮次/工具数/时限预算及取消传播。
4. 在 `core/context/` 实现有界消息构造、工具输出截断标记、项目指令作为不可信任务上下文；在 `tests/fixtures/` 放可编排 FakeModel/FakeTool。Application 层只做依赖组合与 Run 调用。

**先写单元测试**：文本终答；单/多工具；分片 JSON 在完成前零执行；截断与空响应；重复 `call_id`；usage 缺失标 unknown；模型异常；预算超限；审批拒绝；取消；每个非法状态迁移。**集成测试**：Application + FakeModel + FakeTool + 内存仓储完成“请求→工具→结果→终答”的无网络闭环，断言调用顺序和终态。**退出门禁**：假模型闭环稳定，Core 无 SDK、CLI、SQLite 或具体执行器依赖。

## 5. M2 — Model Gateway

**目标**：通过两个真正不同的协议接入模型，并把流统一为 M1 契约。

**编码交付**

1. 在 `src/deity/adapters/model/` 实现 OpenAI-compatible 与 Anthropic 适配器；仅适配请求、响应、流事件、能力和错误，不访问工作区。
2. OpenAI-compatible 支持显式 `base_url/model/api_key` 配置；Anthropic 使用官方 SDK。密钥仅从环境或安全配置注入，不能进入事件、错误或测试快照。
3. 将文本、工具调用开始、参数增量、完成、usage、结束原因、错误统一映射；完整消息结束后才把整批工具调用提交给 Runtime。`finish_reason=length`、断流或不完整 JSON 均不得执行工具。
4. 429/临时 5xx 仅在没有产生本轮副作用时有界重试；401/错误模型 ID 立即失败。取消时关闭底层 stream；不自动跨 provider 重放。

**先写协议/错误测试**：用 SDK mock 或录制的脱敏协议样本覆盖两种协议的文本、单/多工具、分片参数、截断、异常、usage 缺失、能力不足和取消。**集成测试**：两个适配器分别接入 M1 的 FakeTool 闭环，HTTP/SDK 边界用本地 mock，不依赖真实网络。**退出门禁**：同一 Core 契约驱动两种协议，所有错误路径可复现；真实厂商连通性另列可选检查，不代替协议测试。

## 6. M3 — Tool Runtime

**目标**：实现受控的本地 Coding 工具与审批管线。

**编码交付**

1. 在 `src/deity/adapters/tools/` 实现 registry、参数 schema 校验、风险分类、权限策略、审批 broker、dispatcher；在 `src/deity/execution/local/` 实现 `read_file/list_files/glob/grep/apply_patch/write_file/shell/git_status/git_diff`。
2. 执行链为：完整工具调用 → registry → schema → 风险 → 工作区路径及 symlink 检查 → 权限 → 审批 → 执行 → 结果/审计。未知工具、参数错误、拒绝、冲突都返回结构化结果。
3. 工作区内读取默认允许，写入按配置授权，Shell 默认询问，工作区外写和高风险操作默认拒绝。补丁校验现有内容或 hash，避免覆盖用户改动。
4. Shell 明确 cwd、环境白名单、超时、输出上限和取消清理；stdout/stderr 截断必须标记。Git 工具只读，不自动重置、清理或提交。工作区路径校验不宣称为 OS 沙箱。

**先写单元测试**：每个工具覆盖成功、参数错误、权限拒绝、越界、超时、取消；补丁冲突、symlink 越界、重复 `call_id`、输出截断和密钥脱敏另设针对性用例。**集成测试**：临时 Git fixture 中让 FakeModel 读取、搜索、修改、运行测试、查看 diff；断言实际文件变化、审批记录、拒绝后无副作用、取消后进程终止。**退出门禁**：M1/M2 回归通过，工具调用只经统一管线，越界及未授权写入为零。

## 7. M4 — Session & Events

**目标**：落地 SQLite 会话、运行记录和事件，使可见结果有持久化依据。

**编码交付**

1. 在 `src/deity/adapters/storage/` 实现 schema migration 和 Session/Task/Run/Step/Event/Message/ToolExecution/Evidence repositories；在 Application 层提供创建、查询会话和运行的用例。
2. 每 Run 事件 `sequence` 连续且唯一；工具结果与对应事件同事务提交，提交成功后才向 CLI 发布完成事件。`UNIQUE(run_id, call_id)` 防止重复执行记录。
3. 启动时把遗留 `RUNNING/WAITING_APPROVAL` 标为 `interrupted`，历史可查；不自动恢复或重放不确定副作用。使用 WAL、busy timeout；事件 payload 带 schema version，敏感字段脱敏，大输出受限。
4. `aiosqlite` 与单线程写队列的选择按实测一致性与复杂度写 ADR；若迁移改变 V0.2 示意 schema，同步架构文档与迁移测试。

**先写单元测试**：状态持久化、事件序号、重复 `call_id`、脱敏与错误分类。**集成测试**：临时 SQLite 中并发写序号/唯一约束、事务失败回滚、关闭重开后的历史查询、遗留 Run 中断标记、发布晚于提交、重复调用无副作用重放。**退出门禁**：完整执行链的工具结果、事件、终态可查询且相互一致。

## 8. M5 — Evaluation

**目标**：以可执行断言验证完整 Coding Agent 闭环，生成可复核报告。

**编码交付**

1. 在 `tests/fixtures/` 建可复制的 Python/Java 小仓库；在 `evals/cases/` 定义 E01–E08，在 `evals/runners/` 实现隔离复制、固定预算、执行、断言与清理，在 `evals/reports/` 生成 JSON/Markdown 报告。
2. L0 协议单测、L1 工具/存储集成、L2 scripted model 确定性 E2E 为阻断门禁；L3 真实模型基线单独运行，记录 provider/model、版本、调用数、耗时、usage 与费用，缺失值标 unknown。
3. E01 分析只读；E02 增功能；E03 修 Bug；E04 分片工具参数；E05 权限越界；E06 超时取消；E07 模型失败重试；E08 持久化中断。每项用机器断言核对文件、测试退出码、事件与安全边界，预期答案不得进入模型上下文。

**先写 runner 测试**：故意失败的断言必须使评估失败；超时、fixture 隔离和报告缺项有错误结果。**集成/E2E 测试**：在可丢弃 fixture Git 仓库运行 E01–E08，并检查运行前后快照及报告可追溯性。**退出门禁**：L0/L1/L2 全通过；E04–E08 安全门禁 100% 通过，E01–E03 的确定性功能断言全通过。L3 仅作为发布评审基线，不虚构成功率阈值。

## 9. 阶段交接记录模板

每完成一个里程碑，在 PR 或阶段报告中记录：变更文件及边界、先失败后实现的测试、执行命令与退出码、单元/集成/E2E 证据、未运行项、剩余风险、下一阶段依赖。M5 发布报告沿用评估计划中的指标与 `PASS/FAIL/NOT_RUN/UNKNOWN` 口径。

**依据**：[V0.1 项目规划](../01-project/deity-v0.1-plan.md)、[V0.2 核心架构](../02-architecture/deity-harness-v0.2.md)、[开发指南](development-guide.md)、[评估计划](../04-evaluation/evaluation-plan.md)、[AGENTS.md](../../AGENTS.md)。
