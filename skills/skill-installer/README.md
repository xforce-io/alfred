# Skill Installer

通过对话动态安装和管理 Alfred 技能的元技能。

## 特性

- **对话式安装**: 直接告诉 Agent 你的需求，自动搜索并安装技能
- **多种安装源**: 支持注册表、Git、URL、本地路径
- **依赖管理**: 自动安装 pip、npm、brew、uv 等依赖
- **技能市场**: 支持本地和远程技能注册表
- **非侵入式**: 完全基于 Dolphin 的 ResourceSkillkit，无需修改核心代码

## 快速开始

### 通过对话安装技能

```
You: "我需要一个处理 PDF 的技能"

Agent: 让我搜索相关技能...
找到: nano-pdf - PDF 编辑工具

You: "安装它"

Agent: ✓ nano-pdf 安装成功！
```

### 命令行使用

```bash
# 搜索技能
python scripts/search.py "calendar"

# 安装技能
python scripts/install.py calendar

# 从 Git 安装
python scripts/install.py https://github.com/user/skill --method git

# 列出已安装技能
python scripts/list.py
```

## 架构

```
skill-installer (元技能)
├── SKILL.md               # Skill 定义
├── scripts/               # 实现脚本
│   ├── install.py        # 安装逻辑
│   ├── search.py         # 搜索逻辑
│   └── list.py           # 列表逻辑
├── registry-example.json  # 注册表示例
└── README.md             # 本文档
```

## 安装方式

### 1. 从注册表安装（推荐）

```bash
python scripts/install.py skill-name
```

从本地或远程注册表查找并安装技能。

### 2. 从 Git 仓库安装

```bash
python scripts/install.py https://github.com/user/skill --method git
```

克隆 Git 仓库到技能目录。

### 3. 从 URL 安装

```bash
python scripts/install.py https://example.com/skill.zip --method url
```

下载并解压压缩包（支持 .zip, .tar.gz, .tar.bz2）。

### 4. 从本地路径安装

```bash
python scripts/install.py ~/my-skills/skill --method local
```

从本地路径复制技能。

## 技能注册表

### 使用本地注册表

创建 `~/.alfred/skills-registry.json`:

```json
{
  "version": "1.0",
  "skills": {
    "my-skill": {
      "name": "My Skill",
      "description": "Description",
      "version": "1.0.0",
      "source": {
        "type": "git",
        "location": "https://github.com/user/skill"
      }
    }
  }
}
```

### 使用远程注册表

设置环境变量：

```bash
export ALFRED_SKILL_REGISTRY="https://example.com/registry.json"
```

或在 `config/dolphin.yaml` 中配置：

```yaml
skill_installer:
  registry_url: "https://example.com/registry.json"
```

## 注册表格式

```json
{
  "version": "1.0",
  "skills": {
    "skill-key": {
      "name": "显示名称",
      "description": "技能描述",
      "version": "1.0.0",
      "tags": ["tag1", "tag2"],
      "source": {
        "type": "git|url",
        "location": "https://github.com/user/repo"
      },
      "install": {
        "kind": "pip|npm|brew|uv",
        "package": "package-name",
        "bins": ["binary-name"]
      },
      "requires": {
        "bins": ["required-binary"],
        "env": ["API_KEY"],
        "os": ["darwin", "linux"]
      }
    }
  }
}
```

## 依赖管理

支持的包管理器：

| 管理器 | 命令 | 用途 |
|--------|------|------|
| pip | `pip install` | Python 包 |
| npm | `npm install -g` | Node.js 包 |
| brew | `brew install` | macOS/Linux 工具 |
| uv | `uv tool install` | Python 工具 |

安装技能时会自动安装声明的依赖。

## 技能开发

### 创建新技能

1. 创建目录：
```bash
mkdir -p ~/.alfred/skills/my-skill
```

2. 创建 `SKILL.md`：
```markdown
# My Skill

技能描述

## 功能

- 功能列表
```

3. 测试技能：重启 Agent，技能会自动加载

### 发布到注册表

1. 将技能上传到 Git 仓库
2. 在注册表中添加技能信息
3. 提交 Pull Request（公共注册表）或更新私有注册表

## 配置

在 `config/dolphin.yaml` 中添加（可选）：

```yaml
skill_installer:
  registry_url: "https://example.com/registry.json"
  default_method: "registry"
  auto_install_deps: true
```

即使不配置，skill-installer 也能使用默认设置正常工作。

## 故障排除

### 技能安装失败

**原因**: 缺少 git 等工具

**解决**:
```bash
brew install git  # macOS
sudo apt install git  # Linux
```

### 找不到注册表

**原因**: 网络问题或注册表不存在

**解决**: 创建本地注册表
```bash
mkdir -p ~/.alfred
cp registry-example.json ~/.alfred/skills-registry.json
```

### 依赖安装失败

**原因**: 包管理器未安装或权限不足

**解决**: 手动安装依赖
```bash
pip install package-name
npm install -g package-name
brew install formula-name
```

## 工作原理

1. **非侵入式设计**: 作为普通 skill 被 Dolphin 的 ResourceSkillkit 加载
2. **脚本执行**: 通过 Python 脚本执行实际的安装、搜索等操作
3. **标准格式**: 使用 SKILL.md 格式，与 Dolphin/OpenClaw 完全兼容
4. **目录管理**: 将技能安装到配置的 skills 目录
5. **依赖处理**: 调用系统包管理器安装外部依赖

## 与 OpenClaw 兼容性

由于使用相同的 SKILL.md 格式，可以直接安装 OpenClaw 的技能：

```bash
python scripts/install.py https://github.com/openclaw/skill-name --method git
```

或在注册表中引用 OpenClaw 技能仓库。

## 文档

- [设计方案](../../docs/skill-installer-design.md) - 完整的架构设计
- [使用指南](../../docs/skill-installer-usage.md) - 详细的使用说明
- [注册表示例](./registry-example.json) - 参考格式

## 贡献

欢迎贡献技能到公共注册表或改进 skill-installer 本身。

## 许可

MIT License
