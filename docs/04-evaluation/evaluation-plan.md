# Deity V0.1 最小评估体系与验收计划

> 状态：评估方案基线，尚未运行真实 Deity 实现。目标：评估 **Harness 的正确性、安全性与可验证性**，而不只比较模型的文字回答。

## 1. 评估分层

| 层 | 运行方式 | 目的 | 是否阻断合并 |
|---|---|---|---|
| L0 协议单测 | FakeModel / FakeTool | 状态机、流事件、参数聚合、预算 | 是 |
| L1 工具集成 | 临时工作区 + SQLite | 文件、shell、权限、取消、事务 | 是 |
| L2 确定性 E2E | Fixture repo + scripted model | 完整 Coding Loop 的可复现正确性 | 是 |
| L3 真实模型任务 | 固定任务 + 指定 provider/model | 任务完成率、成本、效率 | 发布评审；初期不作为每次 PR 阻断 |

## 2. 八个固定基线用例

| ID | 场景 | 输入/注入 | 机器可验证断言 | 风险 |
|---|---|---|---|---|
| E01 | 代码库分析 | fixture Python/Java repo | 读取工具事件存在；没有写操作；输出引用真实文件 | 低 |
| E02 | 新增简单功能 | 添加函数/REST endpoint | diff 仅触及许可文件；指定单测通过 | 中 |
| E03 | 修复可复现 Bug | 预置失败测试 | 修复前失败、修复后通过；保留原有改动 | 中 |
| E04 | 分片工具参数 | 模拟 JSON 分多 chunk | 完成前无工具执行；完整后恰好执行一次 | 高 |
| E05 | 权限与越界 | `../`、symlink、工作区外写入 | 拒绝、无越界文件变化、审计事件存在 | 高 |
| E06 | Shell 超时与取消 | sleep/长运行进程 | 超时/取消结果正确；受控进程终止；Run 终态正确 | 高 |
| E07 | 模型失败与重试 | 429、401、断流、缺失 usage | 有界重试；认证不重试；无重复副作用 | 高 |
| E08 | 持久化与中断 | 运行中强制结束并重启 | 历史可查、遗留 run 标 interrupted、不自动重放工具 | 高 |

## 3. Fixture 设计

```text
evals/
  cases/
    E01-repo-analysis.yaml
    E02-add-function.yaml
    E03-fix-bug.yaml
    E04-streaming-toolcall.yaml
    E05-permission-boundary.yaml
    E06-cancel-timeout.yaml
    E07-model-failure.yaml
    E08-persistence-restart.yaml
  runners/
  reports/
tests/fixtures/
  python-mini-repo/
  java-mini-repo/
```

每个 fixture 必须可重新创建、包含初始 Git commit、测试脚本和预期断言。运行前复制到临时目录，运行后删除临时目录，不在用户真实项目上进行破坏性评估。评估任务的预期答案和断言不得进入模型上下文。

### 用例 YAML 示例

```yaml
id: E03
name: fix-null-handling
fixture: tests/fixtures/python-mini-repo
prompt: "修复 normalize_user 对 None 输入抛出的异常，并运行测试"
mode: deterministic
limits:
  max_turns: 12
  max_tool_calls: 30
  deadline_seconds: 180
assertions:
  - type: command_exit_code
    command: ["python", "-m", "pytest", "-q"]
    expected: 0
  - type: workspace_boundary
    expected: no_outside_changes
  - type: event_present
    event: ToolCompleted
```

注意：以上 YAML 是**待实现的评估描述格式**，并非已有 runner 支持的正式 schema。

## 4. 关键指标与定义

| 指标 | 定义 | 数据源 |
|---|---|---|
| Task Success Rate | 通过全部强制断言的任务数 / 运行任务数 | runner assertions |
| Safety Violation Count | 未授权执行/越界写/危险命令放行次数 | 工具审计 + 文件系统快照 |
| Tool Error Rate | 非预期失败工具调用数 / 总工具调用数 | tool_executions |
| Duplicate Side Effects | 同一 call_id 重复产生副作用次数 | 工具执行审计 |
| Cancellation Latency | 取消请求到受控执行停止的时间 | monotonic timestamps |
| Avg Turns / Calls | 每任务模型轮次/工具次数均值 | run/step/events |
| Time to Complete | 开始到终态的墙钟时间 | runs |
| Token Usage | provider 报告的输入/输出 token | model usage，缺失标 unknown |
| Verification Coverage | 有实际编译/测试证据的代码修改任务比例 | evidence |

## 5. V0.1 阻断门禁

**硬性门禁（必须 100% 通过）**：

1. E04 不执行不完整工具参数；同一 call_id 不产生重复副作用。
2. E05 所有越界与未授权操作均被拒绝，无工作区外修改。
3. E06 取消/超时有明确终态，受控进程按策略清理。
4. E07 非重试错误不盲重试；已发生副作用不被自动重放。
5. E08 会话和关键事件可查询，异常中断不自动继续执行。
6. 所有 L0/L1/L2 自动化测试通过，且无密钥泄漏。

**功能门禁**：E01、E02、E03 在确定性 scripted model 模式下全部通过；真实模型 L3 记录基线，不对模型成功率设虚构的硬阈值。初期建议同一任务至少重复 3 次（若预算允许），同时报告方差与失败类型。

## 6. 评估执行流程

```text
create clean fixture repo
 → capture baseline git status/filesystem snapshot
 → configure model (fake/scripted or real) + budget
 → run Deity CLI/Application
 → collect event log, tool results, diff, test exit code
 → assert safety + functionality + verification
 → generate JSON + Markdown report
 → cleanup temp workspace
```

### 报告格式（示意）

```json
{
  "case_id": "E03",
  "run_id": "example-run-id",
  "provider": "scripted",
  "model": "fake-model",
  "passed": true,
  "assertions": [{"name": "pytest_exit_0", "status": "pass"}],
  "model_calls": 4,
  "tool_calls": 7,
  "duration_ms": 1250,
  "token_usage": null,
  "verification": "PASS",
  "artifact_paths": ["reports/E03/example-run-id.json"]
}
```

## 7. 真实模型对比原则

比较不同厂商模型时固定 fixture、任务文本、Harness 版本、权限策略、预算与可用工具；记录 provider/model ID、温度/推理参数（若支持）、模型 API 版本和运行日期。区分 **模型能力差异** 与 **Harness 缺陷**；失败需人工归类，不以模型自述作为成功证据。不得在评估中自动暴露生产密钥或真实仓库机密。

## 8. 回归策略与发布判定

- 每次 PR：L0 + L1；关键架构变更增加 L2。
- 每个候选版本：完整 L0/L1/L2 + 至少一轮 L3 基线。
- 修改事件协议、工具权限、状态机、SQLite schema 时必须增加对应回归用例。
- 发布报告明确：通过、失败、跳过、未知；不隐瞒未执行测试。

## 9. 实施任务拆分

1. 实现可复制 fixture 仓库与初始化脚本。
2. 实现 FakeModel 的 scripted stream 与工具响应。
3. 实现评估 runner、断言插件、超时与隔离清理。
4. 实现 JSON/Markdown 报告与 CI 上传 artifacts。
5. 在 M3/M4 之后逐步启用 E01–E08；对每个缺陷补回归用例。

**关联**：`../02-architecture/deity-harness-v0.2.md`、`../03-development/development-guide.md`、`../../AGENTS.md`。

## 10. V0.5 高级编码评估 A01–A12

V0.5 增加 `uv run --locked python -m evals.runners.advanced`，固定覆盖：

| ID | 场景 | 核心断言 |
|---|---|---|
| A01 | Plan 只读 | 写工具不可执行，计划可持久化 |
| A02 | Plan→Build | 独立 Build Run、CLI 闭环、ChangeSet |
| A03 | 计划过期/漂移 | TTL 与工作区变化阻断批准 |
| A04 | Python 符号 | 定义候选、位置与置信度 |
| A05 | Java 符号 | lexical 定义候选与文本引用 |
| A06 | TypeScript/cache | TS/TSX 定位与内容 hash 失效 |
| A07 | 初始 dirty 文件 | 默认拒绝，逐路径显式授权 |
| A08 | Patch 边界 | preimage、CRLF/BOM、二进制、symlink、submodule |
| A09 | 非 Git 工作区 | 文件 hash 基线与 ChangeSet |
| A10 | Verification pass/fail | Evidence 与聚合状态来自实际退出码 |
| A11 | Verification 不确定 | 缺依赖、超时、抖动、取消清理 |
| A12 | 兼容性 | V0.5 开关关闭时旧路径保持通过 |

A01–A12 属于确定性 L0/L1/L2 门禁。真实模型任务、token 与成本仍属于 L3；未运行时必须记为 `NOT_RUN/UNKNOWN`。
