# ADR-021：F4 信任分级、可写委派补丁回收、证据时间线与 replay 产品化

状态：已采纳（F4 / V2.0 主体实施基线）。依据：[融合计划 §F4](../05-roadmap/deity-codex-fusion-plan.md)、V2.0 路线图条目、[ADR-013](adr-013-v0.7-readonly-delegation.md)。分级审批的层次化思路参照 openai/codex 的审批策略分层（模式借鉴，独立实现，见 `docs/06-legal/third-party-notices.md`）。

## 背景

- ADR-013 的委派严格只读：子 Agent 只能读不能改，真实编码任务里「让子 Agent 先改一版看看」无法表达。
- 委派批次此前没有风险分级：只读批次与（未来的）可写批次在授权路径上无区别。
- 运行事实（事件、turn_diff、证据、委派、验证报告）都已落库，但没有一个按时间线组织起来的只读视图；回放只有 `eval replay` 一个入口，藏在评估子命令下。

## 决策

### B3 信任分级

1. 纯函数 `grade_delegation(contracts)`（`core/delegation`）给出批次级等级：全部只读为 `standard`；任一可写为 `elevated`；可写且（工具含 `apply_patch` 或批次预算合计 ≥16 轮 / ≥40 次工具调用）为 `high`。
2. 等级随委派结果 JSON 一并返回父模型（`trust_grade` 字段），并可从存储的委派结果复核。
3. 授权门（`RunService._authorize`）：**可写委派永不自动放行**——`delegate` 调用只要任一任务 `mode="writable"`，在 `auto` 模式下也必须走审批通道（approval_port 或 approve 回调）；无审批通道时拒绝（`writable_delegation_requires_approval`）。`deny` 模式照常拒绝写类调用的规则不变。本门只收紧不放松。

### B6 可写委派 + patch 回收

4. 契约扩展（`core/contracts/delegation.py`）：`DelegationRequest` 新增 `mode: Literal["read_only", "writable"] = "read_only"`（默认值保证旧调用点零改动；digest 因新增字段而变化，仅影响跨版本同 ID 委派的冲突检测，旧存储记录缺该字段时按只读处理）。可写模式的工具上限为 `WRITABLE_DELEGATION_TOOLS = READ_ONLY_TOOLS ∪ {write_file, apply_patch}`；shell/git/MCP 等一律不可委派。`delegate` 工具 schema 的每个任务新增可选 `mode` 字段，默认 `read_only`。
5. 总开关：`[multi_agent] writable`（默认 `false`）。关闭时可写契约在 `_delegate` 入口被拒（`writable delegation is disabled`），与既有 `enabled` 开关正交。
6. 隔离执行：可写子 Run 在**工作区暂存副本**（`WorkspaceStager` 拷贝到工作区外的临时目录）里运行，子 `AgentRequest.workspace` 指向暂存区，工具经 `WritableChildDispatcher` 白名单分发（每次调用复检）。子 Agent 对真实工作区的写入在物理上不可能发生；暂存区在回收后删除。
7. patch 回收：子 Run 结束后以暂存区基线哈希与终态哈希的差异（`collect_manifest`）生成 Deity patch 文本（`application/delegation_patch.py`）：新增→Add File、删除→Delete File、修改→Update File（difflib 分组 hunk；生成后用 `plan_patch` 对基线自校验，校验不过回退整文件替换 chunk，再不过则该文件标记跳过）。二进制/不可解码文件与超限（`[multi_agent] max_patch_chars`，默认 32000）时不附 patch，只报文件名与原因——**不返回不可应用的半截 patch**。
8. 回收交付：patch 文本挂在 `SubAgentResult.patch`（含 `patch_files`）随结果 JSON 返回父模型；父模型用自己的 `apply_patch` 工具在真实工作区应用——应用照常经过父级的审批、Git 守卫与预像锁定，**回收不绕过任何既有门**。同时在子 Run 上记 `delegation_patch` 证据、在父 Run 上记 `DelegationPatchHarvested` 事件（文件列表、字符数、sha256），供时间线与审计读取。`ResultReducer` 超限时先整段丢弃 patch（并在摘要标注），再走既有摘要减半逻辑。

### 证据时间线

9. `application/timeline.py` 提供只读聚合：`run_timeline(repo, run_id)` 把存储事件（含 `turn_diff` 的文件级明细）、证据、委派子项与验证报告按序组织为 `TimelineEntry(seq, kind, title, detail)`；不写库、不重放执行。
10. 表面：CLI `deity timeline <run-id> [--db]` 与聊天命令 `/timeline`（当前运行）。两者都只读。

### /replay 产品化

11. 新增顶层 `deity replay <报告文件|run-id> [--db]`，与 `eval replay` 同一实现（`TraceReplayer`），输出补齐终态、事件数与摘要（文本/工具调用/审批计数）；`eval replay` 保留兼容。

## 不做

- 可写子 Agent 不直连真实工作区、不自动应用 patch、不开放 shell/git 委派（后续里程碑如需，必须另行 ADR）。
- B4（工件/附件协议）与 B7（前端协议扩展）无真实前端需求，本期不推进（融合计划原文即如此约定）。
- 不改 SQLite schema、不改事件协议（`DelegationPatchHarvested` 是新增事件类型，旧读者忽略未知类型）。

## 回退

- `[multi_agent] writable=false`（默认）即回到 ADR-013 纯只读语义；`mode` 字段默认值保证旧契约可读。时间线/replay 为纯读表面，删除无副作用。
