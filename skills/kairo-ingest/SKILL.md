---
name: kairo-ingest
description: Use when scanning Voice Memos or ~/Downloads for XXX-YYMMDD files to register into existing Kairo topics. Triggers include 语音备忘录, Downloads 入库, 扫描备忘录, kairo add 扫描.
version: "1.0.0"
tags: [kairo, ingest, voice-memo, downloads]
---

# Kairo Ingest

扫描语音备忘录和 `~/Downloads` 里名为 `XXX-YYMMDD` 的材料，登记到**已有** Kairo Topic。

两条路径：

- **对话里**：先推荐 Topic/Ref，等确认再 `apply.py`（可带 `kairo step`）。
- **例行任务**：Agent 自动处理**新出现且尚未入库**的材料——Topic = 唯一匹配，否则 `topic_hint`，再否则已有 Topic「未分类」。只 `add`（例行默认 `--no-step`，避免 ASR 撑破 600s turn）。没有新材料则整段回复 `NO_USER_MESSAGE`，不推 Telegram。

## When to Use

- 用户要扫语音备忘录 / Downloads、把 `XXX-YYMMDD` 入库 Kairo
- 例行 isolated routine `kairo-ingest 扫描入库`

## 非目标

- **不** `kairo new` / `tag create` / `kairo run`
- **不**把文件拷进 `demo_agent` 工作区
- **不**自己做 ASR（`kairo step` 才转写；例行默认不做 step）

## 命令

写操作走 `KAIRO_REAL_BIN`（默认 `$HOME/.local/bin/kairo`），**禁止** `demo_agent/bin/kairo`。

```bash
INGEST="$SKILL_DIR/scripts"
ROOT="${KAIRO_SERVE_ROOT:-$HOME/kairo}"

# 例行：新材料自动入库；没有新的则 stdout=NO_USER_MESSAGE
python "$INGEST/ingest.py" --root "$ROOT"

# 只读
python "$INGEST/scan.py" --root "$ROOT"
python "$INGEST/scan.py" --root "$ROOT" --only-new --auto-topic --format text
```

## Agent 流程

### 例行（强制）

1. 只跑 `ingest.py --root "$HOME/kairo"`（不要再手写 scan/apply 流程）。
2. stdout 为 `NO_USER_MESSAGE` → 回复全文只能是这一个词。
3. 否则把 stdout 回执**原样**发给用户（本轮新入库的 title→topic）。不要附已跳过清单。
4. 禁止 `kairo new` / `run`。

### 对话（确认后才写）

1. `scan.py`，清单必须含推荐 Topic。
2. 用户说确认执行后才 `apply.py`。
3. 回执原样发出。

## 推荐规则（scan 已实现）

- 标题/文件名严格 `XXX-YYMMDD`；跳过 `新录音*`
- 语音备忘录读 `CloudRecordings.db` 标题
- 唯一匹配 → `topic`；否则 `topic_hint`（已有 Topic 模糊推荐）；例行再落到「未分类」
- 已入库（标题变体 / 源文件路径 / ingest ledger）→ skip，例行消息里不出现
