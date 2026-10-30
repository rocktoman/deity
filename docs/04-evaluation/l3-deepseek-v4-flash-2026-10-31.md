# DeepSeek 真实模型 L3 基线（2026-10-31）

## 配置与边界

- 使用 OpenAI-compatible Chat Completions，`base_url=https://api.deepseek.com`，请求模型名为 `deepseek-v4-flash`。DeepSeek [官方文档](https://api-docs.deepseek.com/quick_start/agent_integrations/)说明该旧名仍可调用，但现在路由到更新的 Flash 模型；此处记录请求名，不将它表述为独立的旧模型实例。
- 密钥仅从环境变量 `DEEPSEEK_API_KEY` 读取，未写入仓库、配置或报告。
- 每项任务在临时 Git fixture 中运行。E01 禁止写入和 Shell；E02 只批准工作区内写入；E03 只批准工作区内补丁和 pytest 命令。每轮最多 512 输出 token、120 秒总时限；E01 最多 3 轮/2 次工具调用，E02–E03 最多 6 轮/5 次工具调用。

## 可执行断言结果

| 用例 | 实际结果 | 模型调用 | 输入/输出 token | 工具证据 |
|---|---|---:|---:|---|
| E01 只读分析 | PASS；Run completed、工作区干净、回答引用 `calc.py` | 2 | 1820 / 129 | `read_file` 成功 |
| E02 新增功能 | PASS；`multiply.py` 唯一新增源文件，独立断言 `multiply(2,3)==6` | 2 | 1844 / 270 | `write_file` 成功 |
| E03 修复 Bug | PASS；修复前 pytest 退出码 1，模型执行 pytest 与外部复核均退出 0，仅 `calc.py` 的受跟踪内容改变 | 5 | 5799 / 497 | `read_file`×2、`list_files`、`apply_patch`、`shell` 均成功 |

上述 token 数来自实际提供方 usage 事件；费用未计算，记为 UNKNOWN。详细 JSON 报告由本地评估生成在 `evals/reports/L3-deepseek-v4-flash-E01.json` 等文件中，按 `.gitignore` 不提交原始输出。

## 首次尝试与复核

- E02 首次检查误把验收脚本自身生成的 `__pycache__` 算作模型新增文件；模型创建的功能和独立断言实际通过。复核时在外部断言运行前检查 Git 变更，结果 PASS。
- E03 首次以 3 次工具调用为上限，模型在执行自测前达到上限，Run 为 failed；补丁的外部测试已通过。将隔离任务上限提高到 5 次工具调用后，模型自行运行 pytest，Run 与独立复核均 PASS。

本基线验证了这三项真实模型任务，不推断其他任务或后续模型版本的成功率。
