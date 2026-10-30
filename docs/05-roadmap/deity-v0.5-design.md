# Deity V0.5 — Advanced Coding Agent 详细设计

> 状态：Proposed；依赖 V0.1/V0.2 基线；不是已实现或已通过验证的功能。

## 1. 版本目标与范围

**目标：**把基础工具升级为具备计划、语义定位、Git 保护与可核验交付的 Coding 工作流。

**范围控制：**仅引入实现本版本验收所需的依赖和新接口。任何跨层改动必须有 ADR；现有 CLI、FakeModel、基础工具及 SQLite 回归测试必须继续通过。

## 2. 模块与接口设计

### 2.1 Plan/Build 双模式

**实现机制与执行流程**：Plan 模式通过 capability/policy 强制只读，允许搜索与分析但禁止文件写入/可变 Shell；计划采用 goal→steps→risks→verification 的结构化 schema；用户批准计划后生成新的 Build Run，记录 plan_id 与审批事件。

**接口/数据契约与交付物**：ExecutionMode、PlanArtifact、PlanApproval；`deity plan`、`deity build --plan <id>`。

**必须覆盖的失败与边界场景**：模型通过 Shell 绕过只读、计划过期、用户中途更改需求。

**建议实现步骤**：

1. 在 `core/contracts` 定义与具体 SDK 无关的数据类型、错误码、状态及事件 schema。
2. 在 `core`/`application` 实现策略与协调逻辑，适配器负责外部 I/O。
3. 增加 Fake 适配器的确定性单测，覆盖成功、拒绝、异常、取消、重复调用。
4. 接入真实后端并写集成 fixture；在 CLI 提供最小可用入口。
5. 输出审计事件、验证证据、基准结果和回退说明。

### 2.2 Code Intelligence

**实现机制与执行流程**：从 ripgrep/Glob/符号索引开始，按语言增加 tree-sitter 或 LSP adapter；索引缓存按 commit/file mtime/hash 失效；结果带文件、行、符号、置信来源；避免把检索结果当作事实而不复核。

**接口/数据契约与交付物**：CodeSearchPort、SymbolLocation、ReferenceQuery、IndexCache。

**必须覆盖的失败与边界场景**：大型 monorepo、重名符号、索引过期、二进制文件。

**建议实现步骤**：

1. 在 `core/contracts` 定义与具体 SDK 无关的数据类型、错误码、状态及事件 schema。
2. 在 `core`/`application` 实现策略与协调逻辑，适配器负责外部 I/O。
3. 增加 Fake 适配器的确定性单测，覆盖成功、拒绝、异常、取消、重复调用。
4. 接入真实后端并写集成 fixture；在 CLI 提供最小可用入口。
5. 输出审计事件、验证证据、基准结果和回退说明。

### 2.3 Git 与补丁安全

**实现机制与执行流程**：写入前检测 git status；补丁按上下文匹配，检测冲突；区分 Agent 修改与用户原有修改；checkpoint 不默认 commit；禁止自动 reset --hard/clean -fd；可选 worktree 隔离试验。

**接口/数据契约与交付物**：ChangeSet、PatchApplyResult、WorkspaceSnapshot、GitCheckpoint。

**必须覆盖的失败与边界场景**：未提交文件冲突、CRLF、符号链接、git submodule、无 Git 仓库。

**建议实现步骤**：

1. 在 `core/contracts` 定义与具体 SDK 无关的数据类型、错误码、状态及事件 schema。
2. 在 `core`/`application` 实现策略与协调逻辑，适配器负责外部 I/O。
3. 增加 Fake 适配器的确定性单测，覆盖成功、拒绝、异常、取消、重复调用。
4. 接入真实后端并写集成 fixture；在 CLI 提供最小可用入口。
5. 输出审计事件、验证证据、基准结果和回退说明。

### 2.4 Verification Engine

**实现机制与执行流程**：任务完成前按项目识别运行 compile/test/lint；命令白名单与授权、超时、证据收集；输出 PASSED/FAILED/NOT_RUN/INCONCLUSIVE；验证结果来自退出码与产物而非模型声称。

**接口/数据契约与交付物**：Verifier、VerificationPlan、VerificationEvidence、FinalReport；`deity verify`。

**必须覆盖的失败与边界场景**：测试依赖缺失、flaky test、无测试项目、失败结果被误报成功。

**建议实现步骤**：

1. 在 `core/contracts` 定义与具体 SDK 无关的数据类型、错误码、状态及事件 schema。
2. 在 `core`/`application` 实现策略与协调逻辑，适配器负责外部 I/O。
3. 增加 Fake 适配器的确定性单测，覆盖成功、拒绝、异常、取消、重复调用。
4. 接入真实后端并写集成 fixture；在 CLI 提供最小可用入口。
5. 输出审计事件、验证证据、基准结果和回退说明。

## 3. 执行时序（实现基线）

```mermaid
sequenceDiagram
    participant U as CLI/User
    participant A as Application
    participant R as Agent Runtime
    participant P as Policy/Context
    participant X as Adapter/Executor
    participant S as Event Store
    U->>A: Start or Resume Run
    A->>S: Persist intent / run state
    A->>R: Run(request, ports)
    R->>P: Validate policy, budget, context
    P-->>R: Decision / prepared input
    R->>X: Call model/tool/extension
    X-->>R: Typed result or error
    R->>S: Persist events and evidence
    R-->>A: Typed progress / final state
    A-->>U: Render + approval / result
```

**故障规则**：外部副作用执行前记录 intent；结果未知时保留 `unknown` 并要求协调，不得因网络或进程重启而盲目重放。取消、超时、审批拒绝均产生可追踪终态或等待态。

## 4. 开发拆分与顺序

| 迭代 | 工作项 | 产出 | 依赖 |
|---|---|---|---|
| S1 | Plan/Build 双模式：契约→Fake 测试→适配器→CLI | 接口、实现、测试、示例 | S0 / V0.2 ports |
| S2 | Code Intelligence：契约→Fake 测试→适配器→CLI | 接口、实现、测试、示例 | S1 / V0.2 ports |
| S3 | Git 与补丁安全：契约→Fake 测试→适配器→CLI | 接口、实现、测试、示例 | S2 / V0.2 ports |
| S4 | Verification Engine：契约→Fake 测试→适配器→CLI | 接口、实现、测试、示例 | S3 / V0.2 ports |

## 5. 验收标准（可执行）

- [ ] Plan 模式 100% 阻断 fixture 中的写操作与可变 Shell
- [ ] 对 Java/Python/TS 三类 fixture 完成符号定位与变更测试
- [ ] 保留用户未提交修改，冲突时拒绝覆盖
- [ ] 最终报告的验证字段均能关联实际执行事件

## 6. 指标与观测

记录 `run_id`、`step_id`、`event_seq`、版本、模型、配置哈希、耗时、工具调用、失败类别、预算、真实验证证据。默认比较基线：V0.1/V0.2 既有 fixture；新能力不得使安全测试回退。阈值需在真实基线测量后固化到 CI。

## 7. 回退与兼容

新能力默认受配置开关控制；关闭后旧流程仍可运行。数据库变更必须提供 forward migration 与可执行的备份/恢复流程；事件 schema 版本化，旧事件只读兼容。发生严重权限或重复副作用缺陷时立即禁用相关功能并阻断发布。

## 8. 明确不做

不引入 LangChain/LangGraph/Deep Agents Runtime；不提前实现后续版本的 UI/分布式组件；不使用模型自评替代测试；不把本地工作区约束描述为沙箱。

## 统一工程约束（所有版本强制执行）

1. **边界不变**：`interfaces → application → core ports ← adapters`；Core 不依赖 CLI、FastAPI、厂商 SDK、SQLite、MCP SDK、Docker SDK。新功能通过 Protocol 注入；禁止跨层快捷调用。
2. **最小依赖**：只在功能实现的那个版本引入实际需要的依赖。新依赖必须记录用途、许可证、维护状态、替代方案、版本锁定、移除成本和安全风险；优先复用 Python 标准库与已存在组件。严禁为了未来可能的功能提前安装完整技术栈。
3. **安全前置**：任何新工具/协议/执行环境必须复用 `schema → policy → boundary → approval → execute → audit`；不把提示词、Skill、AGENTS.md 或 MCP 返回值当作授权依据。Shell 的工作目录限制不是隔离。
4. **验证前置**：每个功能同步交付确定性单元测试、故障注入、集成测试、评估 fixture、事件证据。不能把基准测试、取消、权限、事件审计拖到 V0.9 才做；V0.9 只是平台化。
5. **演进纪律**：数据库/事件/配置格式必须标注版本，提供迁移或向后兼容策略；破坏性修改先写 ADR。所有阶段都有开关和可回退方案。
6. **状态真实性**：模型的文字不能替代工具执行证据。恢复时对结果未知的副作用标记 `unknown`，不得自动重放。
7. **质量门禁**：`ruff check .`、`ruff format --check .`、`mypy src`、`pytest`，以及该版本新增的集成/安全/评估门禁；如未运行，报告必须明确标记。

## 版本交付模板

每一版本必须提供：设计 ADR、接口与数据模型变更、可运行演示、正常/失败/取消测试、指标报告、回滚操作、文档更新。指标阈值是**建议的初始验收目标**，应在首轮基线测量后经 ADR 校准，不得伪称已经达标。
