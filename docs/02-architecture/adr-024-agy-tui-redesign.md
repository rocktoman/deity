# ADR-024：AGY 风格 TUI 全面重构

> 状态：已采纳（2026-11-27）。
> 关联：`docs/02-architecture/adr-022-interactive-workbench.md`、`docs/02-architecture/adr-023-codex-port-round-2.md`。

## 背景

V1.x 期间 `src/deity/interfaces/tui/interactive.py` 逐步演进为 1,976 行的单文件实现，先后叠加了 Claude Code、Klee、Gemini、Codex 与 LSF 间距补丁，导致：

1. 底部同时堆叠 7+ 行 chrome（`cmd-hint`、`status-top`、`status-divider`、`status-bar`、`model-dialog`、`input-row`、`input-hint`、`disclaimer`、`Footer`），常规终端下严重挤压主对话画布；
2. 弹窗与对话区争抢 flex 空间，而非使用真正的浮层；
3. 命令调度、事件折叠、布局样式和 UI 组件全耦合在 `InteractiveApp` 单类中；
4. `/compact` 仅打印配置开关状态而无法真实压缩会话历史；运行中输入直接被拒绝而不能排队。

## 决策

以 AGY（Antigravity Workbench）的布局秩序、信息密度与交互体验为蓝本，对 `src/deity/interfaces/tui/` 进行模块化重写，同时保留 Deity 的品牌辨识度与 Harness 安全护栏。

### 1. 保留的 Deity 核心基因

- **神之眼启动头像**：保留 `_WELCOME_EMBLEM` 与时间问候欢迎卡片（`WelcomeCard`），作为进入工作台的第一视觉，随对话滚动自然上移。
- **黑白高对比主题**：延续 `theme.py` 中深底纯黑中性灰与浅底 Klee 深绿的双主题体系。
- **五选项审批与 Diff/验证可见性**：审批语义完全复用 `TuiApprovalBridge` + `ApprovalController`，不改五种决定口径，Diff 预览与工具输出/退出码保持可见。

### 2. AGY 风格布局与交互升级

- **极简三段式结构**：
  1. **顶部单行上下文条（`TopContextBar`）**：`◉ Deity · <branch> · <model> · [<mode>]` 与右侧 `Ctx <pct>% · <tokens>`（≥75% 变黄，100% 变红）。
  2. **主工作区（`#workspace-body`）**：左侧纯净对话画布（`#conversation`），右侧可按 `Ctrl+B` 呼出的三标签辅助抽屉（`AuxDrawer`：`[会话]` / `[文件]` / `[任务]`）。
  3. **底部一体式 Composer**：1 行 `FocusBar`（运行态 spinner/当前阶段/耗时/队列数 + 空闲态极简提示）+ 按需显示的 `#cmd-hint` 补全条 + 圆角底色输入框 `ChatInput` + 1 行极简快捷键底条 `#composer-footer`。
- **可折叠工具阶段组（`ToolGroupWidget`）**：连续工具调用聚合成阶段分组，展示折叠箭头、工具计数与耗时，支持点击/命令展开明细。
- **真正的浮层模态框（`ModalScreen`）**：
  - `ApprovalModal`：居中审批/确认浮层，内置可滚动 Diff 预览，保留 `1/2/3/4/Esc` 与 `y/Enter/n/Esc` 单键直达，不再隐藏底部输入框。
  - `ModelPickerModal`：`/model` 交互式过滤选择器。
  - `CommandPaletteModal`：`Ctrl+P` 呼出全局命令与快捷操作面板。
- **输入队列（`InputQueue`）**：Run 进行中提交的新任务进入 FIFO 队列并在 `FocusBar` 显示排队数，本轮结束后自动续跑；`Ctrl+C` 取消时同步清空队列。
- **真实 `/compact` 压缩**：调用 `SQLiteSessionRepository.compact_messages()` 对当前会话历史消息执行确定性压缩（不修改 SQLite Schema），并反馈压缩前后消息数与估算 Token 变化。

### 3. 模块化目录结构

```text
src/deity/interfaces/tui/
├── __init__.py            # 保持不导入 Textual
├── app.py                 # 既有 --run-id 监视台（行为不变）
├── approval_bridge.py     # 审批线程桥（行为不变）
├── commands.py            # 斜杠命令注册与调度（含真实 /compact）
├── interactive.py         # 薄壳 InteractiveApp 与兼容重导出
├── state.py               # 纯 Python 状态树（ToolGroupState / InputQueue / ScrollThrottler）
├── styles.tcss            # 独立 TCSS 样式表
├── theme.py               # 深浅主题与扩展 Token
├── view_model.py          # RunViewModel（行为不变）
├── widgets/               # TopContextBar / WelcomeCard / ToolGroupWidget / ToolBlock / DiffBlock / ChatInput / FocusBar
├── panes/                 # AuxDrawer（会话 / 文件 / 任务三标签）
└── modals/                # ApprovalModal / ModelPickerModal / CommandPaletteModal
```

## 不改项

- 不改 Core 层事件协议、`AgentLoop`、状态机与 SQLite Schema。
- 不改 `deity.interfaces.tui.__init__` 延迟导入约定（未安装 `tui` extra 时 CLI 正常回落行式 REPL）。
- 不改 `TuiApp`（`deity tui --run-id`）与 `RunViewModel` 既有语义。
