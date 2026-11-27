# ADR-026：Phase 3 —— AST/词法 Repo Map、安全模型路由降级与后台 Shell 会话管理

> 状态：已采纳（2026-11-27）。
> 关联：`docs/02-architecture/adr-011-v0.5-advanced-coding.md`、`docs/02-architecture/adr-019-f2-shell-rules-compact.md`、`docs/02-architecture/adr-025-phase2-reasoning-rewind-commands.md`。

## 背景

在 Phase 2 落地思考流、时间旅行与自定义命令后，Phase 3 聚焦三项研发提效能力，同时严格遵守“按需引入依赖、零离线阻断、不破 Core 架构边界”的原则：

1. **Repo Map 代码地图**：帮助用户与模型快速俯瞰仓库核心符号结构，减少盲目 `grep`/`read_file` 轮次。
2. **模型路由与故障切换（Fallback）**：在主模型遇到限流/连接异常且尚未输出语义结果时，安全降级到备用模型或按轻量任务路由。
3. **后台 Shell 会话可视化管理**：将既有 `ShellSessionManager` 的持久会话状态暴露到 TUI 抽屉与 `/ps`、`/kill` 命令中。

## 决策

### 1. 零新增依赖的混合 Repo Map（`adapters/code_intelligence/repo_map.py`）

- Python 文件使用标准库 `ast` 精确提取类、方法、函数名、参数签名与起止行号；TypeScript/JavaScript/Java/Go/Rust 等文件复用轻量词法规则提取核心定义。
- 严格有界扫描（跳过隐藏/系统/构建目录、`max_files`、`max_file_bytes`、`max_chars`、`onerror` 静默容错），保证在任意目录安全运行。
- 通过 `/map` 命令在工作台实时生成并展示紧凑代码地图；架构解耦，后续可按需挂载 tree-sitter 后端。

### 2. 遵循 `ModelProvider` Protocol 的安全路由（`adapters/model/router.py`）

- 新增 `RoutedModelProvider`，组合主路由、可选轻量 `fast_route` 与有序 `fallbacks`。
- **安全不变量**：仅当抛出 `ModelFailure` 且 `not exc.semantic_output`（尚未产生任何工具调用或正文输出）且错误属于可切换类别（`model_rate_limited`、`model_connection`、`model_http` 或 `retryable=True`）时，才自动切换到下一备用路由；一旦已开始输出语义内容，绝不盲目切换重放，避免重复副作用。
- 记录最近使用的路由与降级轨迹，供 `/route` 命令查询。

### 3. 后台 Shell 会话观测与终止

- `ShellSessionManager` 新增 `list_sessions()` 只读快照接口。
- `ToolPipeline` 与 `RunService` 暴露 `active_shell_sessions()` 与 `kill_shell_session(name)`。
- TUI 右侧 `AuxDrawer`「任务」页实时展示运行中的后台 Shell 会话；新增 `/ps` 与 `/kill <会话名>` 斜杠命令。

## 不改项

- 不新增第三方运行时依赖，不改 Core 层协议与 SQLite Schema。
