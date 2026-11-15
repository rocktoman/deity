# CHANGELOG

格式遵循 Keep a Changelog；版本号遵循 semver（见
`docs/03-development/compatibility-policy.md`）。

## [Unreleased]

### 新增

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
- Windows 类型检查：三处 POSIX 专属调用加平台守卫——Skill 有界读取在
  Windows 上按不可用处理（不做弱化替代）、本地进程超时终止在 Windows
  退化为终止进程本体、Docker 创建参数在 Windows 省略 `--user`；
  POSIX 行为不变，`mypy --platform win32` 通过。

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
