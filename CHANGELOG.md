# CHANGELOG

格式遵循 Keep a Changelog；版本号遵循 semver（见
`docs/03-development/compatibility-policy.md`）。

## [Unreleased]

### 新增

- 交互工作台黑色主题（ADR-022 修订）：深色主题改为黑色基底（背景
  #000000、面板深灰、强调色白色/浅灰），界面不再出现绿色；神之眼徽记
  保留形状、颜色改为中性。Klee 深绿 #1E4C35/#2E7D5B 品牌色废弃，浅色
  主题暂不动。
- Codex 行为移植（ADR-022 修订）：每轮末 diff 块（可点击折叠，行号 +
  +/- gutter，新增行浅灰底、删除行红底）；工具行动词标签（读/搜/列/
  $/写/改/删 + 目标）；等待行带工作耗时（12s · Ctrl+C 取消）；新增
  斜杠命令 /diff /new /review /export /copy /theme /archive（行式与
  工作台共用语义，/copy 与 /theme 仅工作台）；/status 加分支行；新增
  CLI 顶层命令 `deity review [--base <ref>]`；输入框 @ 文件补全（Tab
  补全）。出处见 docs/06-legal/third-party-notices.md 的 Codex 条目。
- 模型失败呈现诊断细节（ADR-022 修订）：「运行失败」不再只有失败
  代码——ModelFailure 新增可选 detail，界面呈现为
  `model_http（HTTP 404: 服务端消息摘要）`；detail 只带 HTTP 状态码
  与截断（200 字符、去换行）的服务端错误消息，连接类错误只带异常
  类型名，绝不含密钥、请求头或请求体。
- 交互工作台 Gemini 风格对齐（ADR-022 修订）：输入框去框化（上下
  两条圆角细线 + 底色条，取代完整边框盒子）；输入框上方两行式
  StatusRow（等待/提示行 + 淡色分隔线 + 模式行，右 Tip 窄屏隐藏；
  模式副提示随当前模式变化；上下文占用 0.75 变黄、满额变红，窄屏
  只显示 %）；用户消息加 `> ` 前缀；等待行仅左竖线 + 斜体；/help
  与审批面板标题改用 SectionHeader 顶线样式；审批时折叠输入区。
  神之眼徽记与 Klee 深绿品牌色保留，不引入 Gemini 品牌元素。
- Codex 行为移植第二轮（ADR-023）：工具输出预览带 (exit N)（live
  `tool_call_completed` 事件新增可选 `output_preview`/`exit_code`
  字段，Live-only、不落库、不改 SQLite 结构）；审批里看 diff（改文件
  工具在审批请求上附预期 diff，行式卡片与工作台审批面板均彩色渲染，
  新增行浅灰底、删除行红底）；/model 弹窗（工作台内列表 + 搜索/过滤，
  从 KNOWN_MODELS 取数，选中切换本次会话模型；行式保持行内列表）；
  /usage（本会话用量汇总：input/output tokens、轮次、工具次数、上下
  文占用；无账号配额后端，只展示本地可得数据）；/rename <新标题>（重
  命名当前会话，/sessions 与侧栏显示新标题）；/sandbox（查看沙箱后端/
  策略/启用/网络/可用性，read-only/workspace-write 会话内切换；无
  bwrap/非 Linux = 明确不可用，不回退宿主机执行）。出处见
  docs/06-legal/third-party-notices.md 的 Codex 条目。
- 斜杠命令帮助与模型切换（ADR-022 修订）：/help 改为每条命令占一行
  （由命令注册表生成，行式与工作台共用；行式打印禁用 markup，避免
  [call_id] 等方括号被 Rich 当样式吞掉）；/model 无参数时列出本提供方
  的可选模型（当前项标记 ●），openai_compatible 类端点无固定列表时明
  确提示；/model <名称> 仍只改会话内模型名、不换提供方。
- 交互工作台 Ctrl+C 退出（ADR-022 修订）：Ctrl+C 语义改为分级——
  待决确认先取消、Run 进行中取消 Run、空闲时直接退出应用（与
  Ctrl+Q 等价）；Esc 保持只取消不退出。欢迎头与 /help 的按键提示
  同步改为「Ctrl+C 取消/退出」。
- 交互工作台 Klee 风格对齐 P0（ADR-022 修订）：新增自定义主题
  模块（深浅两套，品牌色取 Klee 深绿 #1E4C35、四级文字色层级、
  边框色中性灰化）；欢迎头/输入行/审批面板边框圆角化；用户消息
  加极淡品牌色底块、系统消息分级配色、工具行 Rich 分段着色；
  Markdown 代码块底色与标题层级定制；神之眼徽记随主题重新着色。
- 交互工作台 Claude Code 式观感对齐（ADR-022 修订）：启动欢迎头
  （神之眼字符徽记 + 版本/模型/工作区，边框块呈现，与原一行欢迎
  消息合并）；输入框改为带 ❯ 提示符的边框盒子，状态行移至输入框
  正下方并固定显示当前模式与 (shift+tab 切换) 提示；新增
  Shift+Tab 模式循环（普通 → 计划 → 自动接受 → 普通，状态转移与
  /plan、/auto、/normal 共用实现，切入自动接受仍走审批桥显式
  确认、不静默绕过）；工具调用改为一行式条目 + ⎿ 缩进结果行，
  去掉工具块边框面板感。
- 交互工作台（ADR-022，解冻并取代 V1.1-M4 的 TUI 冻结决策）：
  全屏 Textual 界面成为 TTY 下的默认交互入口——无子命令的
  `deity`、`deity run`（不带任务）与 `deity tui` 进入同一工作台：
  底部输入框（Enter 提交、Ctrl+J 换行、输入历史）、流式对话区
  （Markdown 文本块 + 可展开的工具节点）、界面内审批面板（经审批
  桥复用既有 ApprovalController：允许一次/本次会话允许/shell
  命令模式允许/永久允许存规则/拒绝，语义与 CLI 卡片一致）、
  状态栏（模型、工作区、当前工具、上下文占用、Run 状态）与
  斜杠命令子集（/status /cost /tools /clear /compact /plan
  /build /normal /model /auto /sessions /resume /doctor）。
  非 TTY/管道保持行式 REPL；`deity tui --run-id` 保留为接入已有
  Run 的监视台（行为不变）。`[tui] enabled` 默认改为 true，
  显式设 false 仍可整体关停（退出码 2 / TTY 路由回落行式）。

### 修复

- 子进程最小环境补齐平台必需变量（Windows 的 SYSTEMROOT/WINDIR/
  TEMP 等、POSIX 的 HOME/TMPDIR）：此前 Windows 上任何联网 shell
  命令都会因 Winsock WinError 10106 失败，F2 的 dev server 端到端
  用例在 macOS/Windows CI 上即因此失败（macOS 侧另叠加慢 runner
  时序因素，该用例已改为轮询等待式断言并在会话早死时带出输出）。

### 新增

- F4（融合计划 V2.0 主体，ADR-021）：可写委派 + patch 回收——
  `[multi_agent] writable = true` 时，`delegate` 任务可声明
  `mode: "writable"`，子 Run 在工作区暂存副本里执行（工具上限
  读 + write_file/apply_patch），结束后把改动回收为 Deity patch
  随结果返回父级，父级经自有 apply_patch（审批 + Git 守卫）应用；
  真实工作区不被子 Run 触碰。回收记 `delegation_patch` 证据与
  `DelegationPatchHarvested` 事件。
- F4（ADR-021）：信任分级——委派批次按 standard/elevated/high
  分级并随结果返回；可写委派永不自动放行，无审批通道时拒绝。
- F4（ADR-021）：证据时间线——`deity timeline <run-id>` 与聊天
  `/timeline` 按序呈现事件（含 turn_diff 文件明细）、证据、委派
  与验证报告；新增顶层 `deity replay`（与 `eval replay` 同实现）。
- F3（融合计划，ADR-020）：Linux bwrap 沙箱档——`[sandbox]
  backend = "bwrap"` 时 shell 经 bubblewrap 命名空间隔离执行
  （只读根、禁网、工作区按 `policy` 只读/可写绑定，staging 复制
  + 宿主侧哈希回收）；bwrap 不提供 CPU/内存/PID 配额，能力报告
  如实标注；无 bwrap/非 Linux 时明确不可用，绝不回退宿主。
- F3（ADR-020）：会话 UX——会话自动标题（首个任务首行）、归档
  标记（`deity archive-session`，只改元数据）、从 run 最新前沿
  分叉（`deity runs fork` / 聊天 `/fork`，可多次分叉独立演进，
  经 resume 继续）；SQLite schema 升至 v7（迁移前备份 .v6.bak）。
- F3（ADR-020）：`deity export <run-id>` 把 run 导出为 JSONL
  （session_meta 首行 + 逐条事件 + summary），SQLite 仍是主存储。
- F3（ADR-020）：项目约束层 `.deity/requirements.toml`——权限、
  沙箱、上限与委派开关只能收紧不能放宽，对所有配置层（含命令行）
  生效，`deity config show` 标注约束来源。
- F2（融合计划 V1.2 下半，ADR-019）：持久 shell 会话——shell 工具
  新增 `session`/`stdin`/`close`/`yield_ms` 参数，可在一次运行内
  跨轮保活进程（dev server、watch、REPL），按游标增量取输出；
  纯轮询免审批，启动与写入仍走审批门；运行结束统一回收，另有
  900 秒空闲回收。
- F2（ADR-019）：持久审批规则——审批决定可沉淀为
  `~/.config/deity/rules.toml`（或 `DEITY_RULES_PATH`）中的
  allow/deny 规则（shell 为命令模式、其余为工具级），ask 路径下
  规则先行表态；审批卡片新增「永久允许（保存为规则）」选项，
  `deity rules list/add/remove` 管理规则文件。
- F2（ADR-019）：compact 结构化摘要——`[run] compact_summary`
  开启后，压缩时用本次运行的模型为被折叠的旧轮次生成结构化
  摘要（`compaction_summary_v1` 消息），与确定性账本并存；摘要
  失败或导致超限时回退纯账本，默认关闭。
- F1（融合计划 V1.2 上半，ADR-018）：apply_patch 新增多文件 patch
  文本形态（`patch` 参数：Add/Delete/Update/Move hunk、`@@` 上下文锚、
  四级模糊定位、先全量校验后落盘；旧 `path`+`old`+`new` 形态保留
  兼容），实现参照 openai/codex apply-patch 文法重新实现，来源声明见
  `docs/06-legal/third-party-notices.md`；Git 守卫对新形态逐文件执行
  同一套预检与预像锁定。
- F1（ADR-018）：只读工具并行——同一轮内连续的只读调用
  （read_file/list_files/glob/grep/git_status/git_diff 及
  find_symbol/find_references）并发执行、结果按原序回注，写调用
  构成屏障；OpenAI 兼容与 Anthropic 适配器能力声明改为支持
  parallel_tool_calls。
- F1（ADR-018）：新增持久化事件 `turn_diff`——每轮工具调用后产出
  本轮文件变更的 unified diff（单文件截断 8,000 字符，二进制只记
  sha256），同时经 live 事件同名投递，为 /undo 与证据时间线提供
  数据前置。shell 的改动不在追踪范围。
- V1.1 立项：`docs/05-roadmap/deity-cli-optimization-roadmap.md`（CLI 优化路线图，
  V1.1 里程碑 M1–M4 / V1.2 / V2.0）。
- V1.1-M1：Qwen 成为一等模型厂商（`provider = "qwen"`，默认模型
  `qwen3.8-omni-flash`，默认端点为 DashScope OpenAI 兼容端点，可用 `base_url` 覆盖）。
- V1.1-M1：新增 `deity init` 命令生成最小可用配置（默认 Qwen；只写密钥环境变量名，
  不写密钥值；已有配置默认拒绝覆盖，`--force` 才覆盖）。
- V1.1-M2：新增进程内事件总线 `InProcessEventBus`（ADR-017）：`run_stream` 与
  `ClientService` 的进程内订阅统一走总线（回放 + live），SQLite 只做持久化、
  跨进程接入与重放；新增 CLI 视图模型 `ChatViewModel`（事件先折叠成状态树，
  渲染器只画树）；工具分发器组装自 `RunService` 抽出为 `ToolPipeline`。
- V1.1-M3：流式渲染增强：Markdown 按段落块渲染（终端流式时开启）、工具节点
  折叠/展开（完成行只显示摘要，新增 `/tools` 命令展开完整参数）、状态条补
  当前工具与上下文占用、长时间无事件时的思考/运行提示。
- V1.1-M4：`deity config show` 分层展示有效配置（每段标注来源层，密钥只查
  环境变量存在性）；TUI 冻结为监视用途并在文档标注（不再新增功能）。

### 变更

- V1.1-M4：shell 工具的「本次会话允许」细化到命令模式（如 `pytest *`，
  按命令首 token 派生），不再放行整个 shell 工具；非 shell 工具不变。

### 修复

- CI：mypy 对可选 extras（prompt_toolkit / textual）加 overrides，未装 extras
  时不再因 import-not-found 阻断 test 矩阵（此前 pytest 步骤被连带跳过）；
  supply-chain 漏洞扫描的依赖导出加 `--no-emit-project`，避免 pip-audit
  在 `-e .` 可编辑行上误报失败。
- Windows 类型检查：三处 POSIX 专属调用加平台守卫——本地进程超时终止
  在 Windows 退化为终止进程本体、Docker 创建参数在 Windows 省略
  `--user`；POSIX 行为不变，`mypy --platform win32` 通过。
- CI test 作业改装 chat/tui extras（`uv sync --locked --dev --extra chat
  --extra tui`）；同时给 5 个依赖 prompt_toolkit 的测试加缺包跳过兜底，
  未装 extras 时跳过而不是失败（装上时照常运行）。
- Windows 运行时兼容（CI Windows 矩阵 pytest 实测修复）：会话所有者探活
  在 Windows 改用 OpenProcess（原 `os.kill(pid, 0)` 报 WinError 87）；
  Git 安全补丁的目录 fsync 在 Windows 跳过（无目录句柄语义）；聊天输入
  的 PromptSession 改为首次提示时懒创建（Windows 无控制台时构造即抛
  NoConsoleScreenBufferError）；`fixture_sha256` 的路径分量统一用 POSIX
  分隔符，仓库加 `.gitattributes` 固定 LF 检出，避免 CRLF 漂移误报；
  evals 运行器与回归门禁的文本读写统一显式 UTF-8（Windows 默认 cp1252
  会编码失败）；崩溃恢复测试在 Windows 用 TerminateProcess 等价注入
  硬终止；发布校验和文件改写字节避免 CRLF 污染。
- Windows 运行时兼容（第二轮，CI 诊断实测修复）：Skill 有界读取补
  Windows 实现（逐段 lstat 拒绝符号链接与重解析点，语义与 POSIX 版
  一致，此前 Windows 上一律不可用）；工件目录 fsync 在 Windows 跳过；
  CLI 入口统一把 stdio 切到 UTF-8 并关闭 Rich 的 legacy Windows 模式
  （其宽度减一会使帮助折行与基线差一字节），evals 运行器包导入时做
  同样处理；测试侧把 SKILL.md 等字节敏感夹具改 LF 字节直写、子进程
  文本模式统一 UTF-8、E03 用例的工具参数改用 json.dumps 构造
  （Windows 路径反斜杠会破坏手拼 JSON）、FIFO 断言仅在 POSIX 测。

## [1.0.0] — 2026-11-09

### 新增

- 冻结公开 API 与兼容承诺：`deity.core.contracts.COMPAT_VERSION = 1`，
  `contracts.__all__` 即公开 API 清单（ADR-015）；事件 `schema_version=1`
  字段只增不减；V0.7 时代 SQLite 库（schema v6）升级后可读、事件可查。
- 发布与供应链：GitHub Release 工作流（打 tag 构建 wheel/sdist、SHA256 校验和、
  SBOM）；CI 矩阵加 `windows-latest`；新增 `supply-chain` job
  （`uv lock --check` + CycloneDX SBOM + `pip-audit --strict` 高危阻断）；
  README 安装节（`pipx install deity` / `uv tool install deity` 与 extras 分组）。
- 安全审查：威胁模型（`docs/02-architecture/threat-model.md`）与 `SECURITY.md`；
  安全阻断测试（路径穿越、越界写、密钥外泄脱敏、MCP 不可信返回值、
  prompt injection 不提升权限）。
- 性能与稳定性：soak 运行器（`python -m evals.runners.soak --minutes 10`，
  RSS 增长超阈值即失败）；Release Gate 看板；升级/回滚指南；
  崩溃注入恢复测试（kill 后终态不回 running，未知副作用标 `unknown` 不重放）。
- 演示脚本 `docs/04-evaluation/v1.0-demo.sh`（安装→配置→分析→修改→验证→恢复，
  FakeModel 离线可跑）。

### 变更

- 版本号由 `0.1.0` 升为 `1.0.0`；评估报告中的 `deity_version` 随之变为 `1.0.0`。

### 已知限制

- 本地工作区限制不是 OS 沙箱；Docker 沙箱是可选隔离后端。
- Windows CI 未在本机验证（本机无 Windows runner），见 V1.0 验收记录 NOT_RUN。
- 示例 A/B 实验 off/on 耗时差是运行波动，不是 compaction 效果结论。

## [0.1.0] — 2026-10-31

- V0.1–V0.7 初始实现（CLI、Agent Loop、双协议模型接入、工具调度、权限、
  SQLite 事件、MCP/Skills/Hooks、Plan/Build、沙箱、只读委派）。
- V0.8 统一客户端协议与 TUI；V0.9 评估数据集、benchmark、A/B 实验与回放门禁。
