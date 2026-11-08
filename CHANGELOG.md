# CHANGELOG

格式遵循 Keep a Changelog；版本号遵循 semver（见
`docs/03-development/compatibility-policy.md`）。

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
