# SECURITY.md — Deity 安全政策

## 默认安全配置

- **最小权限**：读工作区默认允许；写操作按策略授权；Shell 默认询问
  （`shell = "ask"`）；越界与高风险操作默认拒绝。
- **审批**：写文件、补丁、删除、Shell 均须经审批回调；Plan 模式只允许只读工具。
- **边界**：所有文件工具先做路径解析与工作区边界校验；本地工作区限制
  **不是操作系统沙箱**——需要隔离时启用 Docker 沙箱后端。
- **密钥**：密钥只从配置指定的环境变量读取；日志、事件、doctor 输出与
  测试快照均经脱敏（密钥键值与 `sk-` / `ghp_` 等明显 token 模式替换为
  `***` / `[redacted]`）；绝不在日志或 issue 中贴真实密钥。
- **扩展默认关闭**：MCP、Skills、Hooks、TUI、Docker 沙箱、多 Agent 委派
  均默认关闭，显式启用后才生效。
- **审计**：工具调用、审批、事件均持久化到 SQLite，可用
  `deity runs inspect <run-id>` 与 `deity eval replay` 复核。

## 支持版本

| 版本 | 支持状态 |
|---|---|
| 1.0.x | 支持（安全修复） |
| < 1.0 | 不支持，请升级到 1.0.x |

安全修复只发布到受支持版本；升级与回滚见
`docs/03-development/upgrade-rollback-guide.md`。

## 披露流程

如果你发现安全漏洞，请**不要**在公开 issue 中披露细节：

1. 通过 GitHub Security Advisories（仓库页面 → Security → Report a vulnerability）
   提交报告；或发邮件到安全联系邮箱占位 `security@deity-project.example`
   （仓库占位地址，正式发布前替换为真实安全联系邮箱；不要向该占位地址
   发送真实敏感信息，以 GitHub Security Advisories 为准）。
2. 报告请写明：受影响版本、复现步骤（最小 fixture 优先）、影响范围、
   是否有缓解措施。**不要在报告中附带真实密钥、token 或用户隐私数据。**
3. 维护者确认后会：复现并定级、在受支持版本发布修复、在 CHANGELOG 中记录、
   必要时发布安全公告。披露前请给维护者合理的修复时间。
4. 威胁模型与已知残余风险见 `docs/02-architecture/threat-model.md`；
   安全阻断测试见 `tests/unit/test_security_blockers.py`。
