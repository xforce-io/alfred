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
- 未命中已有 Topic 的条目标 **待指定**，等用户从现有列表里选或跳过

## 命令

脚本用绝对路径。写操作走 `KAIRO_REAL_BIN`（默认 `$HOME/.local/bin/kairo`），**禁止**走 `demo_agent/bin/kairo`（写拦截 wrapper）。

```bash
INGEST="$SKILL_DIR/scripts"
ROOT="${KAIRO_SERVE_ROOT:-$HOME/kairo}"

# 1) 只读扫描
python "$INGEST/scan.py" --root "$ROOT"

# 2) 把用户确认后的 JSON 存成计划（可改 topic / action=skip）
# 3) 确认执行后才 apply；未确认只允许 --dry-run
python "$INGEST/apply.py" --plan /tmp/kairo-ingest-plan.json --dry-run
python "$INGEST/apply.py" --plan /tmp/kairo-ingest-plan.json
```

`apply.py` 在 Topic 目录内调用 `add` / `title` / `--to` attach，每个命中 Topic **只 `step` 一次**。

## Agent 流程（强制）

1. 跑 `scan.py`，按 JSON 列清单。对每条写清：来源、推荐 Topic、推荐 Ref 标题、`--occurred`、是否 `--copy`、将执行的命令。
2. `topic_status=unspecified` 或 `topic=null`：**待指定**，不预填 Topic，列出 `kairo list` 已有 slug 供选择，或跳过。
3. `action=skip`（已入库）标明跳过原因，不重复 add。
4. **停下来等确认**。用户本轮没有「确认执行 / 直接执行 / 不用问了」时，禁止 `apply.py`（除非 `--dry-run`），禁止手写 `kairo add` / `step`。
5. 用户可改 Topic（必须是已有 slug）、改成 skip、或给待指定补上已有 Topic。把确认后的 JSON 写到 `tmp/kairo-ingest-plan.json`（agent tmp 目录）。
6. 确认后跑 `apply.py --plan ...`（无 `--dry-run`）。
7. **把 stdout 回执原样作为给用户的通知**（含 add 的 ref、step 结果、待指定未执行、失败原文）。失败不当成功。
8. 不要再 `kairo run`，不要对未确认 Topic 执行。

## 推荐规则（scan 已实现，不要另猜）

- 文件名 / 语音备忘录 **标题** 严格 `XXX-YYMMDD`；跳过 `新录音*`
- 语音备忘录读 `CloudRecordings.db` 标题，磁盘文件是时间戳
- Topic：对已有 slug / topic 名最长匹配；零命中或并列 → 待指定
- 同一 `XXX-YYMMDD` 的录音 + 文档合成一条 Ref（音频作主 form，其余 `--to`）
- 语音备忘录默认 `--copy`；Downloads 默认识路径
- 已入库判定（命中任一即 skip，回执只报条数、不逐条推荐）：Kairo 标题等于 `XXX-YYMMDD` 或其 `XXX-YYYYMMDD` / 下划线变体；源文件 basename 或完整路径已出现在某条 Ref 的 location；或 `.kairo/kairo-ingest-seen.json` 已记录（apply 成功后写入）

## 例行任务

demo_agent 每 30 分钟 isolated routine 会跑本技能。例行路径的**站立授权**仅覆盖「scan 已唯一匹配到已有 Topic」的条目：写 plan 后直接 `apply.py`（add + step），把回执发给用户。无新匹配时回执写明「无已匹配可入库」，避免空转误报成功。

- `topic` 为空 / 待指定：只出现在回执里，**禁止 add**
- 已入库 skip：不重复 add，回执只写「已跳过已入库 N 条」，不要把旧标题再推给用户
- 禁止 `kairo new` / `tag create` / `kairo run`
- 写操作走 `KAIRO_REAL_BIN`，禁止 `demo_agent/bin/kairo`
- 无已匹配条目时仍要回复「无已匹配可入库」+ 待指定清单，不能沉默结束
