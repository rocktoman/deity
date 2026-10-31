# Deity 开发指南 V0.2

> 本文保留初始化与开发规范。当前已有代码实现；实际运行结果以本地测试与评估报告为准。

## 1. 环境与依赖

- Python 3.12+；`uv` 管理依赖与锁文件；Git；ripgrep（`rg`）。
- CLI：Typer + Rich；配置/边界验证：Pydantic；模型：OpenAI SDK + Anthropic SDK；持久化：SQLite（初期可选 `aiosqlite`）；测试：pytest、pytest-asyncio、ruff、mypy。
- 不安装 LangChain、LangGraph、Deep Agents Runtime；不安装 FastAPI（V0.1 不提供 HTTP 服务）。

## 2. 初始化命令（创建代码仓库时执行）

```bash
uv init --package deity --python 3.12
uv add typer rich pydantic openai anthropic aiosqlite
uv add --dev pytest pytest-asyncio ruff mypy
uv run deity --help  # 需先在 pyproject.toml 配置脚本入口并实现 CLI
```

建议 `pyproject.toml` 片段：

```toml
[project]
name = "deity"
version = "0.1.0"
requires-python = ">=3.12"

[project.scripts]
deity = "deity.interfaces.cli.main:app"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
line-length = 100

[tool.mypy]
python_version = "3.12"
strict = true
```

## 3. 包结构与职责

```text
src/deity/
  interfaces/cli/      Typer 命令、Rich 渲染、审批交互
  application/         SessionService、RunService、配置组合
  core/contracts/      请求/响应、Protocol、错误码
  core/runtime/        Agent Loop、预算、取消
  core/state/          状态迁移
  core/context/        消息与 token 预算
  core/events/         DomainEvent
  adapters/model/      OpenAI-compatible、Anthropic
  adapters/tools/      Registry、Dispatcher、Policy
  adapters/storage/    SQLite repositories、迁移
  execution/local/     文件/rg/patch/shell/git 执行器
```

依赖方向与禁止事项见仓库根目录 `AGENTS.md`；详细协议见 V0.2 架构文档。

## 4. 推荐开发顺序

逐阶段的编码交付、先写测试、集成场景和退出门禁见 [M0–M5 开发实施文档](m0-m5-implementation-plan.md)。

V0.3 长任务可靠性能力的协议、开发顺序和验收标准见 [V0.3 Reliable Runtime 实施文档](v0.3-reliable-runtime-implementation.md)。

1. **M0**：建立包、CLI `--help`、配置校验、lint/typecheck/test、CI。
2. **M1**：实现领域 DTO/Protocol + FakeModel/FakeTools + AgentLoop；先用无网络测试跑通工具循环。
3. **M2**：接入两种真实模型协议；解析完整/分片 tool call；加入模型错误分类与流式取消。
4. **M3**：工具 registry、schema、权限 broker、文件/rg/patch/shell/git；越界/取消/超时测试。
5. **M4**：SQLite schema migration、事件、会话历史、进程崩溃遗留 Run 标记。
6. **M5**：固定评估集、报告与门禁，形成 V0.1 release candidate。

每个里程碑均应提交小粒度 PR/commit；不得先搭一堆空目录和接口却缺少可运行闭环。

## 5. 测试命令与门禁

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q tests/unit
uv run pytest -q tests/integration
# 端到端与真实模型评估仅在隔离工作区和明确授权下运行
```

**单测**不调用真实模型 API；集成测试使用临时目录、临时 SQLite、假命令执行器；端到端测试使用可丢弃 fixture Git 仓库。真实模型测试可选且单独配置预算。

V0.3 还须运行 `uv run python -m evals.runners.reliability`。候选版 L3 使用 `uv run python -m evals.runners.l3_candidate`，该命令调用真实 DeepSeek API，单独执行，不进入 PR CI。

## 6. 配置与密钥

建议 `~/.config/deity/config.toml`（用户配置）和可选 `.deity/config.toml`（项目配置，不能覆盖安全策略）。密钥从环境变量或 OS keychain 读取，不写入仓库或事件日志。

```toml
[model]
provider = "openai_compatible"
name = "your-model-id"
base_url = "https://your-provider-endpoint.example/v1"
api_key_env = "DEITY_API_KEY"
context_window_tokens = 8192

[run]
max_turns = 30
max_tool_calls = 100
deadline_seconds = 900
compaction_enabled = true
resume_enabled = true

[permission]
shell = "ask"
workspace_write = "ask"
```

配置示例中的 URL 为占位符。环境变量可用 `DEITY_API_KEY` 等明确命名；不同 provider 的具体密钥映射在适配器中配置，严禁输出到日志。

`context_window_tokens` 必须按所选模型明确配置，不能把示例值当作实际模型容量。提供该字段后，新 Run 默认启用压缩；可用 `compaction_enabled = false` 回退。`max_output_tokens` 默认不设置，OpenAI-compatible 请求不会发送人为的 `max_tokens`；Anthropic 协议要求该字段，适配器在未配置时使用 4096。`resume_enabled = false` 会拒绝显式续跑，原 Run 及 v2 审计事实仍可查询。`deity runs inspect` 会返回安全恢复判定；有未知工具结果、活动租约、工作区漂移、配置变化或预算耗尽时不得自动续跑。

需要连续完成本地编码任务时，可将 `[permission]` 中的 `shell` 与 `workspace_write` 显式设为 `"auto"`。工作区内文件写入不再逐次询问；Shell 自动模式仅运行受限的测试和检查命令，其余命令直接拒绝。默认值仍为 `"ask"`；本地工具权限不是 OS 沙箱。详见 [ADR-008](../02-architecture/adr-008-auto-tool-authorization.md)。

## 7. 开发与评审规范

- Python 类型标注，边界优先 Protocol，避免循环依赖。
- 异步调用必须有超时/取消策略；工具输出限制长度。
- 对所有副作用先鉴权；禁止 `git reset --hard` / `git clean -fd` 等自动清理用户变更。
- 模型返回最终回答不等于测试通过；最终报告按实际证据标记 `PASS/FAIL/NOT_RUN/UNKNOWN`。
- 变更状态机、事件协议、权限默认值、存储迁移时必须补测试并写 ADR。

## 8. Definition of Done

功能实现 + 负路径测试 + 文档/协议更新 + lint/typecheck/test 通过 + 安全评审 + 真实执行证据。无法执行的检查明确标注未运行。
