# Deity 发布清单（V1.0）

> 发布流程见 `.github/workflows/release.yml`；兼容政策见
> [兼容政策](compatibility-policy.md)；升级/回滚见
> [升级/回滚指南](upgrade-rollback-guide.md)。

## 发布前

- [ ] 版本号：`src/deity/__init__.py`、`pyproject.toml`、`uv.lock` 三处一致
      （当前 `1.0.0`），`deity --version` 输出与之一致。
- [ ] CHANGELOG：`CHANGELOG.md` 有本版条目（新增/变更/修复/安全/已知限制），
      破坏性变更（如有）写明替代方案与预告版本。
- [ ] 兼容快照：`tests/unit/test_public_api_compat.py` 通过；
      冻结清单有变时先改 [ADR-015](../02-architecture/adr-015-v1.0-compat.md)。
- [ ] 全部门禁通过：`ruff check .`、`ruff format --check .`、`mypy src`、
      `pytest -q` 全量、各确定性 eval 运行器（runner、reliability、extensions、
      advanced、sandbox `--backend fake`、multi_agent、client、benchmark）、
      `deity eval gate`、`python -m evals.runners.soak --minutes 10`。
- [ ] 安全阻断测试通过且零已知高危绕过（`tests/unit/test_security_blockers.py`）；
      有绕过即阻断发布，记入验收记录。
- [ ] 供应链：`uv lock --check` 通过；SBOM（`sbom.json`）含全部直接依赖；
      `pip-audit --strict` 无已知漏洞（有漏洞先升级/替换依赖后重跑，
      不得静默 `--ignore-vuln`；如必须放行，须在 CHANGELOG 写明漏洞编号与理由）。
- [ ] 验收记录：`docs/04-evaluation/v1.0-candidate-2026-11-09.md` 已写，
      逐项写设计文档第 5 节四条的通过/失败/NOT_RUN、真实测试数字与已知限制。
- [ ] 演示：`docs/04-evaluation/v1.0-demo.sh` 离线可跑，日志存
      `evals/reports/v1.0-demo.log`。

## 打 tag 与发布

- [ ] `git tag v1.0.0`（tag 名与版本号一致，`v` 前缀），`git push origin v1.0.0`。
- [ ] GitHub Release 自动生成：wheel/sdist、`SHA256SUMS`、`sbom.json` 均已上传；
      核对 Release 附件的 SHA256 与本地 `sha256sum -c SHA256SUMS` 一致。
- [ ] Release notes：从 CHANGELOG 摘录，写明兼容矩阵（OS/Python/provider）
      与已知限制（本地工作区限制不是 OS 沙箱；Windows CI 未在本机验证）。

## PyPI 发布（手动步骤，不自动）

- [ ] 本地核对 Release 附件 SHA256 与 `SHA256SUMS` 一致后再上传。
- [ ] `twine upload dist/*`（或 `uv publish`），确认 PyPI 页面版本号与文件一致。
- [ ] `pipx install deity` / `uv tool install deity` 实测安装可用，
      `deity --version` 输出正确。

## 回滚

- [ ] 删 tag 重打需说明：已发布的 Release 不可静默改内容；发现问题先发
      patch 版（如 `1.0.1`），不重写已发布的 tag 与附件。
- [ ] 如需撤回 PyPI 版本：用 `twine` / PyPI 界面 yank 该版本并在
      CHANGELOG 与 Release notes 写明原因；已安装用户按
      [升级/回滚指南](upgrade-rollback-guide.md)回退到上一版本，
      数据库用升级前备份的 `deity.db` 恢复（1.0.0 不新增 schema 迁移，
      旧库可直接打开）。
