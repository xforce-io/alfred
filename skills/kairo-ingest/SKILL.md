---
name: kairo-ingest
description: Use when scanning Voice Memos or ~/Downloads for XXX-YYMMDD files to register into existing Kairo topics. Triggers include 语音备忘录, Downloads 入库, 扫描备忘录, kairo add 扫描.
version: "1.0.0"
tags: [kairo, ingest, voice-memo, downloads]
---

# Kairo Ingest

扫描语音备忘录和 `~/Downloads` 里名为 `XXX-YYMMDD` 的材料，推荐已有 Kairo Topic / Ref，**确认后再** `kairo add` 并对该 Topic `kairo step`，把新材料折进 `understanding.md`。完成后把脚本回执原样发给用户。

## When to Use

- 用户要扫语音备忘录 / Downloads、把 `XXX-YYMMDD` 入库 Kairo
- 用户说「扫描备忘录」「登记到 kairo」「这些录音进哪个 topic」

## 非目标

- **不** `kairo new` / `tag create` / `kairo run`
- **不**把文件拷进 `demo_agent` 工作区
- **不**自己做 ASR；转写由 `kairo step` 完成
- 未唯一命中已有 Topic 的条目标 **待指定**：**必须给出推荐 Topic**（`topic_hint`，只来自已有 slug），等用户确认/改选/跳过；禁止只丢标题让用户填 slug；禁止 `kairo new`

## 命令

脚本用绝对路径。写操作走 `KAIRO_REAL_BIN`（默认 `$HOME/.local/bin/kairo`），**禁止**走 `demo_agent/bin/kairo`（写拦截 wrapper）。

```bash
INGEST="$SKILL_DIR/scripts"
ROOT="${KAIRO_SERVE_ROOT:-$HOME/kairo}"

# 1) 只读扫描
python "$INGEST/scan.py" --root "$ROOT"                 # JSON
python "$INGEST/scan.py" --root "$ROOT" --format text     # 全量（含 skip 统计，仅调试）
python "$INGEST/scan.py" --root "$ROOT" --only-new --format text
# 例行给用户：仅未入库项；没有未入库则 stdout=NO_USER_MESSAGE，不发 Telegram

# 2) 把用户确认后的 JSON 存成计划（可改 topic / action=skip）
# 3) 确认执行后才 apply；未确认只允许 --dry-run
python "$INGEST/apply.py" --plan /tmp/kairo-ingest-plan.json --dry-run
python "$INGEST/apply.py" --plan /tmp/kairo-ingest-plan.json
```

`apply.py` 在 Topic 目录内调用 `add` / `title` / `--to` attach，每个命中 Topic **只 `step` 一次**。

## Agent 流程（强制）

1. 跑 `scan.py`。给用户的清单**必须**含每条推荐 Topic。优先把 `scan.py --format text` 原文发出去。
2. `topic` 为空：**待指定**，用 JSON 的 `topic_hint` / `hint_candidates` 作为推荐（已有 Topic，不是新建）。禁止只列标题让用户自己填 slug。用户确认推荐、改成别的已有 slug、或跳过之后才 add。
3. `action=skip`（已入库）标明跳过原因，不重复 add。
4. **停下来等确认**。用户本轮没有「确认执行 / 直接执行 / 不用问了」时，禁止 `apply.py`（除非 `--dry-run`），禁止手写 `kairo add` / `step`。
5. 用户可改 Topic（必须是已有 slug）、改成 skip、或给待指定补上已有 Topic。把确认后的 JSON 写到 `tmp/kairo-ingest-plan.json`（agent tmp 目录）。
6. 确认后跑 `apply.py --plan ...`（无 `--dry-run`）。
7. **把 stdout 回执原样作为给用户的通知**（含 add 的 ref、step 结果、待指定未执行、失败原文）。失败不当成功。
8. 不要再 `kairo run`，不要对未确认 Topic 执行。

## 推荐规则（scan 已实现，不要另猜）

- 文件名 / 语音备忘录 **标题** 严格 `XXX-YYMMDD`；跳过 `新录音*`
- 语音备忘录读 `CloudRecordings.db` 标题，磁盘文件是时间戳
- Topic 唯一匹配：`topic` 有值，例行可自动 add
- 零命中或并列：`topic` 为空，但 `topic_hint` 仍给已有 Topic 的模糊推荐（更长片段优先，同长取更靠前）；没有重叠则推荐=无
- 同一 `XXX-YYMMDD` 的录音 + 文档合成一条 Ref（音频作主 form，其余 `--to`）
- 语音备忘录默认 `--copy`；Downloads 默认识路径
- 已入库判定（命中任一即 skip，回执只报条数、不逐条推荐）：Kairo 标题等于 `XXX-YYMMDD` 或其 `XXX-YYYYMMDD` / 下划线变体；源文件 basename 或完整路径已出现在某条 Ref 的 location；或 `.kairo/kairo-ingest-seen.json` 已记录（apply 成功后写入）

## 例行任务

demo_agent 每 30 分钟 isolated routine。用户消息**只含尚未入库的条目**（含待指定）；全部已入库则**整段回复必须恰好是** `NO_USER_MESSAGE`（不推 Telegram）。未入库就会继续推，直到 add 成功或用户明确跳过。

1. `python "$SKILL_DIR/scripts/scan.py" --root "$HOME/kairo" --only-new --format text`
2. 若 stdout 为 `NO_USER_MESSAGE`：回复全文只能是 `NO_USER_MESSAGE`，不要解释、不要 apply。
3. 否则：对其中 `topic` 非空的条目 `apply.py`；发给用户的正文 = 该 stdout（可加 apply 回执）。禁止附带已跳过条数。
4. 待指定仍禁止自动 add；禁止 `kairo new` / `run`；写操作走 `KAIRO_REAL_BIN`。
