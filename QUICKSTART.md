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

## 下一步

### 与 Agent 对话

```bash
PYTHONPATH=. python examples/real_agent_demo.py
```

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

祝使用愉快！ 🎉
