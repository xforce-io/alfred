---
name: kairo-ingest
description: Use when scanning Voice Memos or ~/Downloads for XXX-YYMMDD files to register into existing Kairo topics. Triggers include 语音备忘录, Downloads 入库, 扫描备忘录, kairo add 扫描.
version: "1.0.0"
tags: [kairo, ingest, voice-memo, downloads]
---

# Kairo Ingest

扫描语音备忘录和 `~/Downloads` 里名为 `XXX-YYMMDD` 的**新**材料，推荐已有 Kairo Topic，**等用户确认后再 add**。禁止例行自动入库。

## When to Use

- 用户要扫语音备忘录 / Downloads、把 `XXX-YYMMDD` 入库 Kairo
- 例行 isolated routine：发现 **24 小时内** 未入库项时推推荐清单，等确认

## 非目标

- **不** `kairo new` / `tag create` / `kairo run`
- **不**自动 `add`（含例行；禁止 `ingest.py` 无人值守）
- **不**把历史库存一次性灌进 Kairo；例行只看录音/文件时间 **24 小时以内**

## 命令

写操作走 `KAIRO_REAL_BIN`（默认 `$HOME/.local/bin/kairo`），禁止 `demo_agent/bin/kairo`。

```bash
INGEST="$SKILL_DIR/scripts"
ROOT="${KAIRO_SERVE_ROOT:-$HOME/kairo}"

python "$INGEST/scan.py" --root "$ROOT" --only-new --since-hours 24 --format text
```

## Agent 流程

### 例行（强制）

1. `python "$SKILL_DIR/scripts/scan.py" --root "$HOME/kairo" --only-new --since-hours 24 --format text`
2. stdout 为 `NO_USER_MESSAGE` → 回复全文只能是 `NO_USER_MESSAGE`（近两天没有未入库新材料，不推 Telegram）。
3. 否则把该 text **原样**发给用户（未入库新项 + 推荐 Topic）。**禁止 apply / ingest.py / add。**
4. 用户本轮确认推荐（或改选已有 slug / 跳过）后，下一轮对话才 `apply.py`。

### 对话

1. `scan.py`（可全量或 `--only-new`）。清单必须含推荐 Topic。
2. 未说「确认执行」禁止 `apply.py`。
3. 确认后 `apply.py --plan ...`，回执原样发出。

## 推荐规则

- 严格 `XXX-YYMMDD`；跳过 `新录音*`
- 语音备忘录读 App 标题
- 唯一匹配 → `topic`；否则 `topic_hint`；没有重叠 → 推荐=无（仍须用户指定已有 Topic 或跳过）
- 已入库 skip，例行消息不列出
