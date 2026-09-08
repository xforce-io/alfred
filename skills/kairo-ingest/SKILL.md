---
name: kairo-ingest
description: Use when scanning Voice Memos or ~/Downloads for XXX-YYMMDD files to register into existing Kairo topics. Triggers include 语音备忘录, Downloads 入库, 扫描备忘录, kairo add 扫描, 确认入库.
version: "1.1.0"
tags: [kairo, ingest, voice-memo, downloads]
---

# Kairo Ingest

扫描语音备忘录和 `~/Downloads` 里名为 `XXX-YYMMDD` 的**新**材料，推荐已有 Kairo Topic，**等用户确认后再 add**。禁止例行自动入库。

对人只发技能规定的正文。禁止过程旁白、禁止加载 kairo 操作手册、禁止翻 HEARTBEAT.md / 旧 scan_out.json。

## 非目标

- **不** `kairo new` / `tag create` / `kairo run`
- **不**自动 `add`
- **不**把历史库存一次性灌进 Kairo；例行只看录音/文件时间 **24 小时以内**

## 命令

写操作走 `KAIRO_REAL_BIN`（默认 `$HOME/.local/bin/kairo`），禁止 `demo_agent/bin/kairo`。

```bash
INGEST="$SKILL_DIR/scripts"
ROOT="${KAIRO_SERVE_ROOT:-$HOME/kairo}"
```

## Agent 流程

### 例行（强制）

```bash
python "$INGEST/scan.py" --root "$ROOT" --only-new --since-hours 24 --format text
```

1. stdout 为 `NO_USER_MESSAGE` → 回复全文只能是 `NO_USER_MESSAGE`。
2. 否则把该 text **原样**发给用户。禁止 apply / add。
3. 用户确认（或改选已有 slug / 跳过）后，**下一轮**才走确认入库。

### 确认入库（原子）

用户已点名标题和 Topic 后，只跑这两条，然后把 apply 的 stdout **原样**发给用户：

```bash
python "$INGEST/scan.py" --root "$ROOT" --only-new --since-hours 48 --format json > /tmp/kairo-ingest-plan.json
python "$INGEST/apply.py" --plan /tmp/kairo-ingest-plan.json --title "标题-YYMMDD" --no-step
```

- `--title` 可重复；只入库这些标题。plan 里其余条目会 skip。
- 条目已有唯一匹配 `topic` 时不必改 JSON。用户改选 Topic 时，改 plan 里该条的 `"topic"` 再 apply。
- 默认 `--no-step`（避免 ASR/compose 撑爆会话）。用户明确要求 step/run 时再对那个 Topic 单独走 kairo 技能。
- 禁止再 scan 考古、禁止复述步骤。

## 推荐规则

- 严格 `XXX-YYMMDD`；跳过 `新录音*`
- 语音备忘录读 App 标题
- 唯一匹配 → `topic`；否则 `topic_hint`；没有重叠 → 推荐=无（仍须用户指定已有 Topic 或跳过）
- 已入库 skip，例行消息不列出
