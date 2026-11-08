# Deity 升级 / 回滚指南（V1.0）

> 兼容政策见 [兼容政策](compatibility-policy.md)；数据库备份与迁移规则见
> [ADR-015](../02-architecture/adr-015-v1.0-compat.md)；
> 发布流程见 [发布清单](release-checklist.md)。

## 升级前备份

`deity.db` 默认位于 `~/.local/share/deity/deity.db`（可用 `deity runs inspect`
确认实际路径；`--db` 可指定其他位置）。升级前先备份：

```bash
cp ~/.local/share/deity/deity.db ~/.local/share/deity/deity.db.bak.$(date +%Y%m%d)
ls -la ~/.local/share/deity/
```

同时备份配置文件 `~/.config/deity/config.toml`
（与项目级 `./.deity/config.toml`，如有）。

## 前向迁移

1. 安装新版本（`pipx upgrade deity` / `uv tool upgrade deity`，
   或从源码 `git pull && uv sync --locked`）。
2. 运行 `deity --version` 核对版本号，运行 `deity doctor` 检查环境。
3. 首次打开旧库时自动做前向迁移：迁移前在数据库同目录写
   `.vN.bak` 备份并校验备份版本（备份校验失败即拒绝迁移，不会写坏原库）。
4. 用 `deity runs inspect <run-id>` 或 `deity eval replay <run-id>`
   抽查旧 Run 的事件可读。
5. **1.0.0 特别说明**：本版不新增 schema 迁移（schema 仍为 v6），
   V0.7 时代库可直接打开；升级主要是版本号与公开 API 冻结，无数据迁移。

## 回滚

### 应用回滚

```bash
pipx install "deity==<上一版本>" --force   # 已发布版本
# 或从源码：
git checkout v<上一版本>
uv sync --locked
deity --version
```

### 数据库回滚

1. 停止所有正在运行的 `deity` 进程（先 `deity runs inspect` 确认无 running 的 Run；
   有 running 的 Run 先等它结束或取消）。
2. 用升级前备份恢复：

```bash
cp ~/.local/share/deity/deity.db.bak.<日期> ~/.local/share/deity/deity.db
```

3. 如升级时自动生成了 `.vN.bak`（如 `deity.v5.bak`），也可直接用它恢复；
   恢复后用 `sqlite3 ~/.local/share/deity/deity.db "SELECT MAX(version) FROM schema_migrations;"`
   核对版本。
4. 恢复后 `deity doctor` 与 `deity eval replay <run-id>` 抽查事件可读。

### 注意事项

- 迁移只前向，不提供降级迁移：新版本写入的新字段/新表，旧版本打开时
  会忽略未知内容（只读兼容），但旧版本无法理解新版本的新语义；
  因此回滚后应以备份恢复为准，不要直接拿新版本写过的库给旧版本用。
- 崩溃后的 Run 在下次打开时被标记为 `interrupted`，待决工具标为 `unknown`，
  不自动重放；恢复流程见 `tests/integration/test_crash_recovery.py`，
  先 `deity runs inspect <run-id>` 看复核结论（`manual_review` 时须人工确认）。
- 已发布的 Release 不重写 tag 与附件；发现问题发 patch 版（见发布清单）。
