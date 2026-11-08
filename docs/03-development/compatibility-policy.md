# Deity 兼容政策（V1.0）

> 冻结清单见 [ADR-015](../02-architecture/adr-015-v1.0-compat.md)；快照测试见
> `tests/unit/test_public_api_compat.py`；迁移测试见 `tests/integration/test_migration.py`。

## 兼容矩阵

| 维度 | 支持 | 说明 |
|---|---|---|
| OS | macOS、Linux、Windows | CI 矩阵含 `ubuntu-latest`、`macos-latest`、`windows-latest`；Windows 仅跑核心测试（`ruff` / `mypy` / `pytest`），Docker 沙箱 job 仍只在 Linux 跑 |
| Python | 3.12、3.13 | `requires-python = ">=3.12"`；CI 矩阵覆盖 3.12/3.13 |
| Provider | OpenAI-compatible、Anthropic | 经 `ModelProvider` Protocol 接入；新增 provider 走适配器，不改 Core |
| 事件 schema | `schema_version=1` | 字段只增不减；旧事件只读兼容 |
| 配置 | TOML `version = 1` | 未知段报错；段只增不减 |
| SQLite | `schema_migrations` 至 v6 | 前向迁移；打开前备份并校验 |

不在矩阵内的组合（如 Python 3.11、其他 provider 协议）不承诺兼容，
但 Core 的 Protocol 设计允许以适配器方式扩展。

## 语义化版本与弃用窗口

- 遵循 semver：破坏性变更只进主版本号；向后兼容的新功能进 minor；修复进 patch。
- 破坏性变更至少提前一个 minor 版本预告：CHANGELOG 与本文件写明废弃内容、
  替代方案与移除版本；预告期内旧 API 仍可用，不得静默改行为。
- 冻结清单（公开 Protocol、事件字段、配置段、SQLite 迁移规则）的改动
  必须先改 ADR-015，再改快照测试。

## Migration Guide：0.1.0 → 1.0.0

本次升级**无破坏性 API 删除**，变化如下：

1. **版本号**：`deity.__version__` 与 `pyproject.toml` 由 `0.1.0` 改为 `1.0.0`；
   评估报告中的 `deity_version` 字段随之变为 `1.0.0`。
2. **新增常量**：`deity.core.contracts.COMPAT_VERSION = 1`；
   `contracts.__all__` 新增 `COMPAT_VERSION`，其余公开名称不变。
3. **配置段新增**：无新增必填段；`[tui]`、`[doctor]`、`[eval]` 段在 0.1.0 之后
   的版本中已存在，1.0.0 不改其语义；旧配置文件无需修改即可继续使用。
4. **数据库**：V0.7 时代库（schema v6）可直接由 1.0.0 打开，升级过程自动备份
   （`.vN.bak`）并校验；1.0.0 不新增 schema 迁移。升级前仍建议先备份 `deity.db`，
   见[升级/回滚指南](upgrade-rollback-guide.md)。
5. **事件**：`schema_version=1` 不变；旧事件只读兼容，无需迁移写入。

## 支持版本

- 仅支持最新发布的 1.x 版本（当前 1.0.0）；安全修复只发到受支持版本。
- 披露流程与默认安全配置见仓库根目录 `SECURITY.md`。
