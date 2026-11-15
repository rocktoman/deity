# CHANGELOG

格式遵循 Keep a Changelog；版本号遵循 semver（见
`docs/03-development/compatibility-policy.md`）。

## [Unreleased]

### 新增

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
