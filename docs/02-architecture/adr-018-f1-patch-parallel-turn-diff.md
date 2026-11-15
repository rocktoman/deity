# ADR-018：F1 编辑与执行效率——patch 协议扩展、只读并行、per-turn diff 事件

> 状态：已接受（2026-11-15，F1 / 融合计划 A1+A2+A4）。
> 关联：`docs/05-roadmap/deity-codex-fusion-plan.md`（F1 立项依据，§3.1
> A1/A2/A4、§4 里程碑 F1）；`docs/02-architecture/adr-017-in-process-event-bus.md`。

## 背景

融合计划对照 Codex 后确认三处效率短板：`apply_patch` 只有单文件单段
精确替换（`old` 必须恰好出现一次），模型改代码失败率高；同一轮的多个
工具调用全串行，只读调用也不例外，且 OpenAI 兼容适配器把
`parallel_tool_calls` 写死为 False，模型每轮至多发一个调用；改动只有
仓库级 `git diff`，没有"本轮改了什么"的一等概念，/undo 与证据时间线
缺数据前置。

## 决定

1. **apply_patch 协议扩展（A1）**：`apply_patch` 新增 `patch` 参数形态，
   采用 Codex 式文本协议（`*** Begin Patch` … `*** End Patch`，含
   Add/Delete/Update/Move hunk 与 `@@` 上下文锚），实现落在
   `adapters/tools/patch.py`（参照 openai/codex `apply-patch` crate 的
   文法与 seek 分级用 Python 重新实现，见
   `docs/06-legal/third-party-notices.md`）。定位按四级严格度回退：
   精确 → 忽略行尾空白 → 忽略首尾空白 → Unicode 标点归一。执行语义是
   **先全量校验后落盘**：全部 hunk 的定位与新内容计算先在内存完成，
   任一失败则整批不写；落盘阶段的 IO 错误不在此保证内（与 Codex 相同）。
   旧 `path`+`old`+`new` 形态保留兼容（至少一个版本），两形态互斥，
   同时给出判为参数错误。Git 守卫对新形态逐目标文件执行与旧形态相同
   的预检（计划目标、子模块、脏路径、先读后写预像），预像在 prepare
   时锁定、dispatch 时复核，任一目标漂移整批拒绝。
2. **只读工具并行（A2）**：分发器新增可选能力 `supports_parallel(name)`
   （沿用 `prepare`/`after_persist` 的 getattr 鸭子类型约定，未实现即
   视为不支持）。本地只读工具（read_file/list_files/glob/grep/
   git_status/git_diff）声明可并行。AgentLoop 在一轮内把**连续**的
   可并行调用组成批次：批次内先串行完成校验/审批/持久化 claim，再
   并发 dispatch，最后按原调用顺序串行记录与回注；写调用与不可并行
   调用仍按原顺序串行执行，构成批次屏障（写后读必见新内容）。
   OpenAI 兼容适配器的能力声明与请求参数改为允许 parallel_tool_calls；
   其余适配器不变。
3. **per-turn diff（A4）**：新增持久化事件类型 `turn_diff`（events 表
   的 type 列为自由文本，本决定不改 schema）。ToolPipeline 组装的最外层
   叠加 TurnDiffDispatcher：对写工具在 dispatch 前后惰性快照目标文件
   内容（每轮每文件只在首次写前快照一次），AgentLoop 在每轮
   checkpoint 前经可选方法 `flush_turn_diff` 取走本轮变更并由仓储
   `record_event` 落盘，同时经 live emit 发同名事件供 CLI 消费。
   payload 为逐文件的 unified diff（单文件截断上限 8,000 字符，
   二进制/不可解码文件只记 sha256 前后值）。shell 造成的改动不在
   追踪范围（与 Codex 的工具变更追踪口径一致）。

## 兼容与不变量

- 事件协议只增不改：新增 `turn_diff` 一个 kind，旧消费者忽略即可；
  live seq 与持久化 seq 仍不合并（ADR-016）。
- 工具参数只增不改：旧 apply_patch 形态与结果文案（`patched`）不变；
  新形态的结果文案以 `patched` 开头并附文件清单。
- 预算/取消/claim 不变量不变：并行批次中每个调用仍独立 claim、
  独立记录、独立超时；批次内某调用 dispatch 抛错时按既有语义标记
  unknown 并中止 Run（批次内均为只读调用，无副作用残留问题）。
- 配置格式不变：本决定不新增配置键。
