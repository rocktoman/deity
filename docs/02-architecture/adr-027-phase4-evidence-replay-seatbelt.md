# ADR-027：Phase 4 —— 证据时间线/回放产品化、评估用例沉淀与 macOS Seatbelt 沙箱

> 状态：已采纳（2026-11-27）。
> 关联：`docs/02-architecture/adr-012-v0.6-execution-sandbox.md`、`docs/02-architecture/adr-020-f3-sandbox-session-export-requirements.md`、`docs/02-architecture/adr-021-f4-trust-writable-delegation-timeline.md`。

## 背景

Deity 的核心护城河在于“可验证、可审计、可回放与强隔离”，但此前：
1. `/timeline` 被限制在行式 CLI 使用，工作台缺少原生证据时间线、只读回放（`/replay`）与将真实任务一键沉淀为评估用例（`/eval-save`）的入口；
2. 本地轻量沙箱仅支持 Linux `bwrap`，macOS 本地开发机在不启动 Docker 时缺少进程级系统沙箱后端。

## 决策

### 1. TUI 原生证据与回放闭环

- **`/timeline [run-id]`**：从 `_CLI_ONLY_COMMANDS` 移出，成为工作台一等命令；缺省自动取最近一次 `run_id`，调用 `application/timeline.py` 的 `run_timeline()` 渲染完整事件、文件变更、委派与验证报告时间线。
- **`/replay [run-id]`**：调用 `TraceReplayer().replay_sqlite()` 以只读方式重演历史 Run（绝不执行写入或网络副作用），在工作台汇总终态、事件数、工具列表与 schema 兼容标记。
- **`/eval-save <case-id>`**：将最近一次 Run 的任务提示词、模型与事件摘要导出为 `evals/cases/<case-id>.yaml` 评估用例骨架（严格校验文件名，默认拒绝覆盖已有文件）。

### 2. macOS 原生轻量沙箱后端（`SeatbeltExecutionBackend`）

- `BackendKind` 新增 `SEATBELT = "seatbelt"`（纯加法扩展）。
- 新增 `src/deity/adapters/execution/seatbelt.py`，复用 `WorkspaceStager` 与 `collect_manifest`，通过 macOS `sandbox-exec -p <profile>` 执行：
  - 严格 SBPL 策略：默认 `(deny default)`、强制 `(deny network*)`（拒绝 `network != "none"` 与 `FULL_ACCESS`）；
  - `READ_ONLY` 模式禁止一切工作区写入（仅放行 `/dev/null`）；
  - `WORKSPACE_WRITE` 模式仅放行 staging 目录、`/tmp`、`/private/tmp` 与 `/dev/null` 写入；
  - 诚实声明 `BackendCapabilities`（不冒充 CPU/内存/PID 硬件配额隔离）。
