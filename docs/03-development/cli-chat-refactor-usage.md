# CLI 聊天用法

交互会话（`deity` 或 `deity run`，无任务参数时进入）的命令、审批与
回退说明。安装增强输入：`pip install "deity[chat]"`（prompt_toolkit
3.0.53，BSD-3-Clause）；未安装时回落到 `input()`，功能一致。

## 输入

- 历史：上下方向键翻查本会话输入历史（`~/.local/share/deity/chat_history`，
  最多 500 条，可配 `[chat] max_history`）。
- 多行：Enter 提交，Alt+Enter 换行，Esc 清空当前行。
- 补全：`/` 补命令，`/build ` 补计划 ID，`@` 补工作区相对路径。
- 取消：运行中第一次 Ctrl-C 取消本轮并回到提示符，取消过程中再按一次
  退出（退出码 130）；输入态 Ctrl-C 回到提示符。
- 非终端（管道/重定向）或 `TERM=dumb` 时用 `input()` + 编号选择，
  输出为整段打印（不流式）。

## @ 引用

- `@文件`：把工作区内文件内容拼进任务；越界、超大（默认 2MB，
  可配 `[chat] mention_max_bytes`）、不存在时报错且不发送。
- `@符号`：按名字查工作区符号定义，把定义位置拼进任务（需开启
  code intelligence）。
- 邮箱里的 `@` 不算引用。

## 斜杠命令

| 命令 | 说明 |
| --- | --- |
| `/help` | 查看可用命令 |
| `/plan` | 进入持续计划模式（需开启 plan_build_enabled 与 git_guard_enabled） |
| `/normal` | 返回普通模式；自动接受模式下同时退出 /auto |
| `/build [计划ID] [--allow-dirty 路径]` | 执行已有计划；无 ID 时用选择器列出本会话可 Build 的计划 |
| `/skill [名称]` | 选择 Skill，只影响下一条任务 |
| `/mcp` | 查看并选择 MCP 工具（仅发现，不调用），只影响下一条任务 |
| `/resume [运行ID]` | 恢复中断的运行；无参数时列出本会话最近的运行与恢复决策 |
| `/sessions` | 列出历史会话，选中后切换（计划模式与待用选择清空） |
| `/compact` | 查看上下文压缩状态（见下） |
| `/clear` | 清屏，不删除 SQLite 历史 |
| `/cost` | 本会话轮次、工具调用数与 token；无 usage 事件时明确写“token 未接通”，不编造数字 |
| `/model [名称]` | 无参数查看当前模型，有参数切换（仅本次会话内生效，下轮任务生效） |
| `/status` | 模型、模式、会话、轮次、token、上下文消息数（最近 8 条口径）、审计拒绝条数 |
| `/auto` | 进入/退出自动接受模式（见下） |
| `/doctor` | 运行环境检查（复用 `deity doctor`），只打印不退出会话 |
| `/exit`、`/quit` | 退出交互 |

## 审批

工具调用（写文件、改文件、删文件、shell）走三选一卡片：

```
[1] 允许一次  [2] 本次会话都允许  [3] 拒绝（默认）
```

- 卡片展示工具名、参数摘要、diff（write_file/apply_patch）或命令全文
  （shell），以及风险提示。
- `[2] 本次会话都允许` 只在内存、只对（工具名，工作区）生效，会话结束
  即失效；不写磁盘、不跨会话共享。
- 权限策略仍由配置决定：`deny` 直接拒绝，`auto` 直接放行（shell 的
  auto 还受内置安全命令白名单限制），只有 `ask` 才会出卡片。
- 计划审批同样走卡片：`confirm_build` 展示计划卡片后一次确认；
  脏文件授权一张卡片集中确认全部待授权路径（一次确认，全部同意才执行，
  否则保留计划；与旧的逐路径确认结果一致——首个拒绝即保留）。

## 自动接受模式（/auto）

- 进入条件：权限已全部为 `auto` 时直接进入；否则必须在提示后输入
  `yes` 显式确认。进入时打印明确提示，不得静默放宽权限。
- 生效范围：本会话所有 `ask` 策略的工具调用直接放行；`deny` 策略仍拒绝。
- 退出：`/auto` 再按一次或 `/normal`，恢复 `ask`（之前选过的
  “本次会话都允许”仍保留到会话结束）。

## ! shell 直通

- `!命令` 在工作区直接执行一条 shell 命令，不写入会话历史。
- 执行前走与工具调用完全一致的权限决策（`RunService._authorize` 的
  shell 分支）：`deny` 拒绝，`auto` 只放行内置安全命令，`ask` 出审批卡片。
- 引用工作区外路径的命令先行拒绝（启发式静态检查：绝对路径在工作区外、
  含 `..` 且解析后逃出）；拒绝事件记入会话内审计（`/status` 显示条数）。
- 超时（默认 30 秒，可配 `[chat] shell_timeout_seconds`）终止并提示；
  输出超过上限（默认 16000 字符，可配 `[chat] shell_max_output_chars`）
  截断。

## /compact（降级实现）

上下文压缩没有独立的触发入口：`compaction_enabled` 只是每轮 Run 的
配置旗标，由 AgentLoop 在运行内做上下文压缩。`/compact` 只报告当前
开启状态，不删除 SQLite 历史、不伪造一次压缩。

## /resume

- `/resume <运行ID>`：恢复决策与执行都走 `RunService`（`inspect()` 判定
  `resume` 才继续）；失败只打印，不退出会话。
- `/resume`（无参数）：列出本会话最近 5 个运行及其恢复决策；有可恢复的
  运行时用选择器挑一个恢复。

## 配置（`[chat]` 段，默认值等于旧行为）

```toml
[chat]
enabled = true              # false 时恢复旧 input() 循环
history_file = ""           # 为空时用默认路径
max_history = 500
multiline = true
status_line = true
streaming = true
approval_default = "ask"
mention_max_bytes = 2000000
shell_timeout_seconds = 30
shell_max_output_chars = 16000
```

## 回退

- `[chat] enabled = false`：恢复旧 `input()` 循环（无历史/补全/流式），
  其余命令与审批逻辑不变；现有交互测试覆盖该路径。
- 卸载 prompt_toolkit 后：自动回落 `PlainInput`，无报错。
