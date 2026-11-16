# ADR-020：F3 安全分级与会话 UX（bwrap 沙箱 / 会话分叉归档 / JSONL 导出 / 配置约束层）

状态：已采纳（F3 实施依据，2026-11-16）。依据：`docs/05-roadmap/deity-codex-fusion-plan.md` §4 的 F3 范围（B2 + A6 + A7 + B5）。沙箱分级语义参照 openai/codex 的 SandboxPolicy 三档（Apache-2.0，`codex-rs/protocol/src/protocol.rs`），用 Python 重新实现；来源声明见 `docs/06-legal/third-party-notices.md`。

## 决策

### 1. B2：SandboxPolicy 三档 + Linux bwrap 后端

- `core/contracts/execution.py` 新增 `SandboxPolicy` 枚举：`read-only` / `workspace-write` / `full-access`，`SandboxSpec` 增 `policy` 字段（默认 `workspace-write`，与既有 Docker 路径行为一致）。`BackendKind` 增 `bwrap`。
- 新增 `adapters/execution/bwrap.py`：仅 Linux + 系统 `bwrap` 可用时 `available=True`；argv 固定为 `--die-with-parent --new-session --unshare-user/pid/ipc/uts/net --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp` + 工作区按 policy 以 `--ro-bind`（read-only）或 `--bind`（workspace-write）挂到 `/workspace` + `--clearenv` + 仅注入 allowlist 环境变量。执行前把工作区复制到 staging（复用 `WorkspaceStager`，与 Docker 同款边界：拒绝 symlink/特殊文件/秘密文件），产物以宿主侧哈希清单回收（`collect_manifest`），不信任沙箱内自报结果。
- 诚实边界：bwrap 不提供 CPU/内存/PID 配额强制，`BackendCapabilities` 对应项如实为 False；`SandboxService` 对 bwrap 只要求进程隔离、只读根、禁网三项，Docker/Fake 维持原全量要求。`full-access` 不是沙箱，bwrap 后端对该 policy 一律拒绝（fail-closed）。非 Linux 或无 bwrap 时 capabilities 不可用，调用方必须明确报错/降级提示，绝不静默回退宿主执行。
- 配置：`[sandbox] backend` 允许 `"bwrap"`；`required=true` 时 backend 可为 docker 或 bwrap；bwrap 不要求 image。RunService 的 shell 路由在 backend 为 docker/bwrap 时都走沙箱路径（bwrap 时 network 必须为 none，本里程碑不做沙箱内联网）。

### 2. A6：会话标题 / 归档 / 分叉（SQLite schema v7）

- schema v7（forward-only，迁移前按既有惯例备份 `.v6.bak`）：`sessions` 增 `archived INTEGER NOT NULL DEFAULT 0`；v2 时代的 `one_successor_per_run` 全量唯一索引改为部分唯一索引（仅 `decision='resume'` 行唯一），使同一父 run 可以有多个 fork 子代而 resume 仍保持单后继。`sessions.title` 列 v1 已存在，本次真正启用：创建 run 时以首个任务 prompt 的首行（截断 80 字、经 `_redact`）自动写入空标题会话。
- `sessions()` 返回 `title` 与 `archived`，默认不含已归档；`archive_session(session_id, archived=True)` 切换标记（只改元数据，不删任何历史）。
- 分叉：`create_forked_run(parent_run_id)` 以父 run 最新 checkpoint（无 checkpoint 时用其 context messages 合成）创建一个新的后继 run（同 session/task），状态 `interrupted`、lineage kind=`fork`、带一份独立复制的 checkpoint 与 context messages；fork 不受 resume 的「父 run 只能有一个后继」约束，可多次分叉，两个分叉互不影响（消息行独立复制）。分叉产物经既有 `RunService.resume()` 继续执行，语义与 resume 一致（安全检查在 resume 时统一执行）。父 run 无任何消息时拒绝分叉。

### 3. A7：JSONL 导出

- 新增 `application/run_export.py`：`export_run_jsonl(repo, run_id) -> str`。首行 `{"type":"session_meta", ...}`（run/session/workspace/model/status/started_at），其后每条事件一行 `{"type":"event","seq":...,"event_type":...,"payload":...}`，末行 `{"type":"summary", ...}`（事件数、证据数、委派数）。导出只读 SQLite，不执行任何工具；内容沿用存储层已脱敏的 payload。CLI：`deity export <run-id> [--db] [--out]`，默认输出到 stdout。格式参照 Codex rollout 的「SessionMeta 首行 + 逐条条目」结构（Apache-2.0，`codex-rs/rollout/`），字段为 Deity 自有定义。

### 4. B5：项目约束层 requirements.toml（只能收紧）

- 工作区 `.deity/requirements.toml` 是独立于普通配置分层的**约束层**：在 `ConfigResolver.resolve` 完成常规合并后应用，只能收紧不能放宽：
  - `[permission]`：`shell` / `workspace_write` 按 `auto < ask < deny` 的严格度取更严者；
  - `[sandbox]`：`required=true` 可把未启用沙箱的配置强制为 required（并要求已启用沙箱后端满足，见下）；`network` 只能收紧为 `none`（约束写 none 时有效配置网络强制 none）；`backend` 约束只能与有效值相同或在有效值为 local 时指定 docker/bwrap（升级隔离），不能把 docker 降为 local；
  - `[run]`：`max_turns` / `max_tool_calls` / `deadline_seconds` 取更小者；`resume_enabled` 只能 true→false；
  - `[multi_agent]`：`enabled` 只能 true→false；`max_children` / `max_depth` 取更小者。
- 约束文件字段/类型非法时整体报错（与配置无效同一口径，不泄露内容）。`config show` 增加 `[requirements]` 段展示命中的约束来源。约束的应用结果进入有效配置，RunService 无需感知约束层存在。

## 回退

- bwrap：`[sandbox] backend` 切回 docker/local 即停用；v7 迁移只加列，旧二进制读 v7 库时多余列无害，必要时恢复 `.v6.bak`。
- 约束层：删除 `.deity/requirements.toml` 即完全失效。
- 导出与分叉均为新增只读/追加路径，停用不影响既有数据。
