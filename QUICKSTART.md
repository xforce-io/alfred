# EverBot 快速开始

5 分钟上手 EverBot！

建议确认 `config/dolphin.yaml`（或 `~/.alfred/dolphin.yaml`）里已启用 `system_skillkit`，否则 `_read_file/_read_folder` 等工具可能不可用。

## 前置：安装

```bash
git clone <repo-url> alfred
cd alfred
bin/setup            # 自动创建 venv、安装依赖、建目录
```

> 需要 Python 3.10+。如果系统默认 Python 不是 3.10+，可以 `PYTHON=python3.12 bin/setup` 指定。

## 第 1 步：初始化 Agent

```bash
./bin/everbot init my_first_agent
```

输出：
```
Agent 工作区已初始化: my_first_agent
路径: ~/.alfred/agents/my_first_agent
已注册到配置: ~/.alfred/config.yaml
```

> `init` 会自动创建工作区并注册到 `~/.alfred/config.yaml`，无需手动编辑配置。

## 第 2 步：自定义行为规范（可选）

编辑 `~/.alfred/agents/my_first_agent/AGENTS.md`：

```markdown
# My First Agent

## 身份
你是一个友好的助手。

## 核心职责
1. 回答问题
2. 提供建议

## 沟通风格
- 友好、简洁
```

## 第 3 步：设置心跳任务（可选）

编辑 `~/.alfred/agents/my_first_agent/HEARTBEAT.md`：

```markdown
# 心跳任务

## 待办
- [ ] 每天早上 9 点问候用户
- [ ] 检查天气预报
```

## 第 4 步：启动

```bash
# 一键启动（后台启动 daemon + web）
./bin/everbot start

# 或前台启动（方便查看日志）
./bin/everbot start --foreground

# Web 界面地址
# http://0.0.0.0:8765
```

## 第 5 步：自检（推荐）

```bash
./bin/everbot doctor
```

## 测试心跳

等待心跳触发（或修改 `interval: 1` 设置为1分钟），你会在日志中看到：

```
[my_first_agent] 开始心跳
[my_first_agent] 心跳结果: ...
```

查看心跳日志：
```bash
tail -f ~/.alfred/logs/heartbeat.log
```

## 连接 Telegram Bot（可选）

### 1. 创建 Bot

在 Telegram 找 `@BotFather` → 发送 `/newbot` → 按提示创建，拿到 bot token。

### 2. 设置环境变量

```bash
export TELEGRAM_BOT_TOKEN="你的token"
```

> 建议写入 `~/.bashrc` 或 `~/.profile` 持久化。

### 3. 编辑配置

```bash
vim ~/.alfred/config.yaml
```

在 `everbot:` 下添加 `channels` 部分：

**单 Bot（最简配置）**：

```yaml
everbot:
  channels:
    telegram:
      enabled: true
      bot_token: "${TELEGRAM_BOT_TOKEN}"
      default_agent: "my_first_agent"
      # allowed_chat_ids: ["123456789"]  # 可选，限制允许的用户
```

**多 Bot（每个 Agent 独立 Bot）**：

```yaml
everbot:
  channels:
    telegram:
      - name: alice-bot
        bot_token: "${TELEGRAM_BOT_TOKEN}"
        default_agent: "alice"
      - name: coding-bot
        bot_token: "${TELEGRAM_CODING_BOT_TOKEN}"
        default_agent: "coding-master"
```

> 多 Bot 模式下，每个 Bot 有独立的 token、独立的绑定关系、独立的默认 Agent。需要分别在 @BotFather 创建。

### 4. 重启

```bash
./bin/everbot restart
```

### 5. 在 Telegram 中使用

打开你的 bot，发送 `/start my_first_agent`，然后就可以直接对话了。

常用命令：
- `/start <agent>` — 绑定 Agent
- `/new` — 清除对话历史
- `/heartbeat` — 查看最近心跳结果
- `/help` — 查看帮助

---

## 下一步

### 查看所有 Agent

```bash
./bin/everbot list
```

### 查看配置

```bash
./bin/everbot config --show
```

## 常用命令

```bash
# 创建新 Agent
./bin/everbot init <agent_name>

# 列出所有 Agent
./bin/everbot list

# 启动守护进程
./bin/everbot start

# 前台启动（用于调试）
./bin/everbot start --foreground --log-level DEBUG

# 运行测试
python -m pytest tests/ -v

# 运行示例
PYTHONPATH=. python examples/everbot_demo.py
```

## 故障排除

### 问题 1: 心跳不触发

检查：
1. `config.yaml` 中 `heartbeat.enabled: true`
2. 当前时间在 `active_hours` 范围内
3. `HEARTBEAT.md` 不为空

### 问题 2: Agent 创建失败

检查：
1. `agent.dph` 文件存在
2. Dolphin SDK 已安装：`pip install dolphin-sdk`
3. 查看错误日志

### 问题 3: 找不到模块

```bash
# 重新运行安装脚本
bin/setup

# 或手动安装依赖
source .venv/bin/activate
pip install -r requirements.txt
```

## 完整示例

`~/.alfred/config.yaml`:
```yaml
everbot:
  enabled: true
  default_model: gpt-4

  agents:
    daily_assistant:
      workspace: ~/.alfred/agents/daily_assistant
      model: gpt-4
      heartbeat:
        enabled: true
        interval: 60
        active_hours: [7, 23]
        max_retries: 3
```

现在你的 Agent 会：
- 每 60 分钟触发一次心跳
- 在 7:00-23:00 之间活跃
- 失败时重试最多 3 次
- 使用 GPT-4 模型

祝使用愉快！

---

## 创建专用 Bot

> 基础 `init` 创建的是通用 Agent。当你需要一个**专注于特定领域**的 Bot（如编程、投资分析），应按以下流程创建专用 Bot。
>
> 核心思路：缩窄 Bot 的职责范围，让即使能力较弱的模型也能通过结构化工具完成复杂任务。

### 第 1 步：初始化

```bash
./bin/everbot init coding-master
```

这会：
- 创建工作区 `~/.alfred/agents/coding-master/`
- 自动注册到 `~/.alfred/config.yaml`
- 生成默认的模板文件

### 第 2 步：定义身份（SOUL.md）

编辑 `~/.alfred/agents/coding-master/SOUL.md`，将通用身份改为专用身份：

```markdown
# coding-master 的灵魂

## 身份
我是 coding-master，专注于代码开发、审查和调试的编程专家。

## 人格特征
- 严谨、注重代码质量
- 遇到不确定时，先读代码再下结论
- 永远通过工具执行，不输出代码块让用户手动跑

## 核心价值
- 所有代码工作必须通过 coding-master 工具链完成
- 行动先于解释
- 一次只做一件事，做完再做下一件
```

### 第 3 步：定义行为规范（AGENTS.md）

编辑 `~/.alfred/agents/coding-master/AGENTS.md`，聚焦于编程职责：

```markdown
# coding-master 行为规范

## 身份
你是 coding-master，一个专注于代码工作的编程专家。

## 核心职责
1. 代码开发（feature delivery）
2. 代码审查（code review）
3. Bug 调试（debugging）
4. 代码分析（analysis）

## 工作方式
- 所有代码工作**必须**通过 coding-master 技能的 $CM 命令完成
- 第一步永远是 `$CM lock --repo <name> --mode <mode>`
- 通过 `_bash` 工具调用执行命令，**绝不**输出代码块让用户手动运行
- 完成后执行 `$CM unlock` 释放锁

## 权限与工具
- `_bash`: 执行命令（包括 $CM 命令）
- `_python`: 执行复杂逻辑
- `_read_file` / `_read_folder`: 读取文件

## 限制
- 严禁执行破坏性命令（如 `rm -rf /`）
- 重要操作前先告知用户
```

> **关键设计**：对于专用 Bot，AGENTS.md 应该把核心工作流直接写进去，而不是依赖模型主动调用 `_load_resource_skill()` 加载技能。这样弱模型也能正确执行。

### 第 4 步：配置模型（可选）

编辑 `~/.alfred/config.yaml`，为 Bot 指定专用模型：

```yaml
everbot:
  agents:
    coding-master:
      workspace: ~/.alfred/agents/coding-master
      model: kimi-code          # 可选：指定模型，不设则用 default_model
      heartbeat:
        enabled: false          # 编程 Bot 通常不需要心跳
```

模型解析优先级：
1. `agents.<name>.model` — Agent 专属模型
2. `everbot.default_model` — 全局默认
3. Dolphin config 中的 `default` — 最终兜底

### 第 5 步：关联技能

技能有三种作用域，按优先级从高到低：

| 目录 | 作用域 | 说明 |
|------|--------|------|
| `~/.alfred/agents/<name>/skills/` | Agent 专属 | 仅该 Bot 可用 |
| `~/.alfred/skills/` | 全局共享 | 所有 Bot 共享 |
| `<repo>/skills/` | 仓库内置 | 随代码分发 |

对于专用 Bot，建议将核心技能放到 Agent 专属目录：

```bash
# 将 coding-master 技能复制为 Bot 专属
cp -r skills/coding-master ~/.alfred/agents/coding-master/skills/
```

> 也可以直接使用仓库内置的技能（不复制），专用 Bot 一样能发现它。复制的好处是可以针对该 Bot 定制技能内容。

### 第 6 步：验证

```bash
# 查看已注册的 Agent
./bin/everbot list

# 环境自检
./bin/everbot doctor

# 启动服务
./bin/everbot start
```

### 第 7 步：从 Channel 接入

Bot 创建完成后，可从任意已启用的 Channel 访问：

**Web**：连接时指定 `agent_name=coding-master`

**Telegram（共享 Bot）**：在已有 Bot 中发送 `/start coding-master` 切换 Agent

**Telegram（独立 Bot）**：为专用 Bot 创建独立的 Telegram Bot：

```yaml
# ~/.alfred/config.yaml
everbot:
  channels:
    telegram:
      - name: alice-bot
        bot_token: "${TELEGRAM_BOT_TOKEN}"
        default_agent: alice
      - name: coding-bot
        bot_token: "${TELEGRAM_CODING_BOT_TOKEN}"
        default_agent: coding-master
```

```bash
# 设置环境变量后重启
export TELEGRAM_CODING_BOT_TOKEN="从 @BotFather 获取的 token"
./bin/everbot restart
```

**CLI 心跳**：`./bin/everbot heartbeat --agent coding-master`

> Channel 只是接入方式，不影响 Bot 本身的能力。同一个 Bot 可以同时被多个 Channel 访问。

### 工作区文件一览

```
~/.alfred/agents/coding-master/
├── SOUL.md          # 身份定义 → 注入 system prompt
├── AGENTS.md        # 行为规范 → 注入 system prompt
├── USER.md          # 用户画像 → 注入 system prompt
├── MEMORY.md        # 长期记忆（运行时按需加载）
├── HEARTBEAT.md     # 心跳任务（运行时按需加载）
├── agent.dph        # Dolphin Agent 定义
└── skills/          # Agent 专属技能目录
    └── coding-master/
        └── SKILL.md
```

### 设计原则

1. **职责聚焦**：专用 Bot 的 SOUL.md 和 AGENTS.md 只描述一个领域，不混入无关技能
2. **工具驱动**：把核心工作流写进 AGENTS.md，让模型按流程调用工具，而非依赖模型的推理能力
3. **Channel 无关**：Bot 的能力由工作区文件和技能决定，与接入渠道无关
4. **模型可替换**：通过 `config.yaml` 随时切换模型，不影响 Bot 定义
