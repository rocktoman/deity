# ADR-019：F2 长任务与审批资产——持久 shell 会话、持久审批规则、compact 结构化摘要

> 状态：已接受（2026-11-15，F2 / 融合计划 B1+A3+A5）。
> 关联：`docs/05-roadmap/deity-codex-fusion-plan.md`（F2 立项依据，§3.1
> A5、§3.2 B1、§4 里程碑 F2）；`docs/02-architecture/adr-018-f1-patch-parallel-turn-diff.md`。

## 背景

融合计划对照 Codex 后确认三处长任务短板：shell 工具是一次性子进程，
dev server、watch 型长测试、交互式程序都活不过一次调用；审批决定只
存在于会话内存（V1.1-M4 的命令模式），关掉终端即清零，用户要为同类
命令反复出卡片；compact 只有确定性账本（事实清单），没有叙事摘要，
长会话压缩后模型要靠零散事实重建任务脉络，容易"丢魂"。

## 决定

1. **持久 shell 会话（B1）**：`adapters/execution/` 新增
   `session_process.py`（`ShellSessionManager`），落在执行适配层。
   shell 工具新增可选参数：`session`（会话名）、`stdin`（向会话写
   入）、`close`（终止会话）、`yield_ms`（本次调用收集输出的等待窗
   口，默认 1000ms，上限 60s）。语义参照 Codex unified_exec 的进程
   会话模型（id→进程、write_stdin、分段取输出）用 Python 重新实现：
   - 无 `session` 时行为与现状完全一致（一次性子进程）。
   - 带 `session` 且会话未存活时用 `argv` 启动持久进程（最小环境、
     独立进程组，与一次性执行同款）；存活时 `argv` 必须为空，
     本次调用按游标增量返回新输出（不重复），附状态行
     （running / exited <code>）。
   - 会话是 **run 级资源**：在同一次运行内跨轮存活（这是它存在的
     意义），运行结束由 `ToolPipeline.close_sessions()` 统一回收；
     单次调用被取消只停止等待，不杀会话进程；另有空闲回收（默认
     900 秒无活动自动终止，下一次操作时惰性检查）。
   - 审批不放松：启动与 stdin 写入都走既有审批门；只有纯轮询
     （无 argv、无 stdin、无 close）免审批——它不能执行任何新
     东西，只是读取已批准进程的输出。
   - 会话的全部可观测历史（启动命令、每段输出、退出码）都在工具
     结果里，而工具结果本来就进入事件流与检查点，可回放；进程本
     身是运行时状态，不做持久化复活（run 结束即回收，与上条一
     致）。
   - 输出缓冲有界滚动（128K 字符，丢弃计数随结果报告），单次结
     果仍受分发器 `max_output_chars` 约束。

2. **持久审批规则（A3）**：`application/` 新增 `approval_rules.py`
   （`ApprovalRules`）。规则文件为 TOML，默认位于
   `~/.config/deity/rules.toml`（与 config.toml 同目录；经
   `DEITY_RULES_PATH` 可覆盖），条目为
   `[[rule]] tool / pattern / effect`：shell 规则的 pattern 是与
   M4 会话模式同语义的命令模式（fnmatch，如 `pytest *`），其余工
   具为工具级规则（pattern 为空）。判定顺序：配置策略 deny 仍是
   硬线（规则不能推翻）；ask 路径下先查规则，deny 规则拒绝、
   allow 规则放行、未命中才落到交互审批/审批端口。文件损坏时按
   空规则处理并保留错误信息（`deity rules list` 可见），不让规则
   文件故障阻断运行。保存为原子替换（临时文件 + replace）。
   amend 全流程：审批卡片新增第 4 个选项"永久允许（保存为规
   则）"（shell 记录命令模式、其余工具记录工具级规则），保存后
   同步进会话允许表；`deity rules list/add/remove` 提供查看与管
   理。命令文本提取与模式派生（`shell_command_text` /
   `command_pattern_for`）由 CLI 层下沉到本模块，CLI 改为再导
   出，保持分层（interfaces → application）。

3. **compact 结构化摘要（A5）**：`core/context/reliable.py` 的
   `ContextCompactor` 新增可选 `summary` 入参。压缩发生且存在被
   折叠的旧轮次时，账本消息之后追加一条
   `compaction_summary_v1` 消息（结构化 JSON：kind + summary 文
   本，经 `sanitize_text` 脱敏）；摘要缺省时若旧轮次中已有上一代
   摘要则原样结转。账本仍是确定性审计层（轮次、来源索引、未解
   决失败、证据 id、已改文件），摘要只是叙事层，两者并存、账本
   可查不变。摘要来源文本由 `summary_source()` 给出（旧轮次的
   角色标注渲染 + 上一代摘要，工具内容截断，总量上限 48K 字符
   超限截头部）。摘要提示词模板参照 Codex compact 模板的结构
   （目标/进展/关键决定/已改文件/未解决/下一步）重新编写，见
   `docs/06-legal/third-party-notices.md`。
   AgentLoop 在 `request.compact_summary` 开启时才生成摘要：用
   本次运行自身的模型端口发一次无工具调用（输出上限 2048
   tokens、60 秒超时且不超过运行截止时间），**任何失败都回退
   纯账本压缩**（现状行为），摘要导致超限时同样丢弃摘要重试。
   开关经 `[run] compact_summary` 配置（默认关——摘要引入额外
   模型调用与不可复现性，默认开启会改变既有行为与成本，opt-in
   是与本仓确定性优先立场一致的选择）。已知取舍：摘要调用的
   token 不计入运行的用量上限核算（有独立的输出/时间上限兜
   底），在后续证据链工作中再统一计量。

## 不变量

- 一次性 shell 的既有语义（超时杀进程组、取消传播、输出上限）
  不变；会话进程的终止同样走进程组 TERM→KILL。
- 规则不能推翻配置层 deny 策略；deny 规则优先于 allow 规则。
- compact 在摘要关闭/失败/超限时与现状逐字节等价（纯账本）。
- 三项均为增量能力：不带新参数的 shell 调用、不带规则文件、
  未开 compact_summary 的运行，行为与 F1 后一致。
