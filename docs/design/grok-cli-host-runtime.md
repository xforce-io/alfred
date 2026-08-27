# L1：demo_agent 宿主 spawn grok CLI

- 状态: Approved（目标会话授权推荐方案）
- 最后更新: 2026-08-27
- 关联: 本机 Alfred 从火山引擎 HTTP 切到 grok CLI；不改 milkie

## 背景

`demo_agent` / `_reflector` 经 milkie sidecar 打火山引擎 OpenAI 兼容端点（`glm-5.2`）。该账号停用。xAI HTTP（`XAI_API_KEY`）无额度。本机 `grok login`（OAuth）可用。kairo / researcher 以宿主 spawn `grok -p` / `--prompt-file` 为 runtime。milkie #251：`agent-cli` 由宿主启动，milkie 不 spawn grok。

## 目标与非目标

**目标**

- `demo_agent` 对话与心跳 turn 由本机 grok CLI headless 驱动，经既有 `AgentProvider.run_turn` → `_progress` 交付回复。
- grok 子进程环境不含 `XAI_API_KEY`，使用 `~/.grok/auth.json` OAuth。
- 其它 agent（如 `coding-master`）仍走 milkie HTTP。

**非目标**

- 不改 milkie 去 spawn grok，不实现 milkie `agent-cli` 生命周期。
- 不使用 `https://api.x.ai/v1`，不消耗 `XAI_API_KEY` team credits。
- 不要求 milkie 对等：SSE token 流、`skill_list` / `run_command`、sidecar sandbox、milkie trace HTML。
- 不把 Alfred skill 迁到 grok marketplace；grok 用自带工具 + 工作区已有文件。
- 不迁移 `coding-master`。

## 思路与折衷

- **选择**：在 Alfred `AgentProvider` 后新增 `GrokCliProvider`，kairo 同款：写 prompt 文件、injectable runner、解析 JSON `text`、产出 llm `_progress`。放弃把 grok 当 OpenAI completions 塞进 milkie gateway——CLI 是完整 agent，不是 tool_calls 端点。
- **选择**：`everbot.agents.<name>.runtime: grok-cli` 显式路由；未设则 milkie。放弃用模型名猜测 runtime。
- **放弃**：本地 HTTP 代理包装 grok CLI 给 milkie——双层 agent、无原生 tool_calls、冷启动更差。
- **放弃**：xAI HTTP——403 无额度。

折衷：grok 是 agent（自带工具、冷启动约 20s、默认 system 很大）。「能 work」= 能交付非空回复，不是 milkie 工具调用对等。

## 边界

- In：`GrokCliProvider`、per-agent `runtime` 路由、`provider_for` 按 handle 分发、demo_agent/_reflector 配置、status 展示 grok-cli、子进程擦除 `XAI_API_KEY`。
- Out：milkie 源码、coding-master、xAI HTTP、grok marketplace 技能重写。

## 主路径与关键状态

- 空：`runtime` 未设 → milkie，行为不变。
- 成功：用户/心跳发一句 → 宿主 `grok --prompt-file … --output-format json` → JSON `text` 进 `_progress` → Telegram/web/心跳看到回复。`./bin/everbot status` 生效模型为 `grok-cli` 而非 `glm-5.2`。
- 错：grok 不在 PATH / OAuth 失效 / JSON `type=error` → `RuntimeError`，不伪装成「(无响应)」；不得落到火山或 xAI HTTP 401/403。
- 不做的界面：不为 grok-cli 新增 Web 设置页。

## 测试意图

- **E2E**：`./bin/everbot restart` + `status` 两次，demo_agent 为 grok-cli；一条 ping turn 回复含 pong（CLI/OAuth 不可用则记录真实失败，不编 transcript）。
- **Integration**：N/A（不引入新外部服务；live 验证走本机 grok）。
- **Unit**：注入 fake runner，断言 argv、子进程无 `XAI_API_KEY`、`_progress` 含 pong。

## 是否需要 L2

否。无新公共 HTTP/CLI 契约；`runtime` 为既有 agent 配置块上的可选键；无新存数/权限模型。实现细节归本变更。

## 关联

- milkie #251 模型连接契约（host spawn CLI）
- kairo `GrokProvider`、researcher `GrokCliAdapter`
- 术语：见 `docs/glossary.md` grok-cli
