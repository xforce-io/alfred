---
name: trace-review
description: 对话式复盘/求证一次 milkie run —— 看"这一轮怎么跑的、调了什么工具、命中什么证据、答案的数据来源"。当用户说"复盘上一轮""你刚那个数怎么来的""这个结论怎么得到的""看下执行过程/命中证据""求证一下""上一轮为什么慢/失败"时使用。
version: "0.1.0"
tags: [trace, observability, review, provenance, debugging, milkie]
---

# Trace Review

复盘一次 milkie run 的执行过程,用于**对话式求证**:把某轮对话的「用户问题 → 执行步骤(工具调用 + 命中证据)→ 最终答案」摊开,让用户核验结论的数据来源、定位慢/失败的原因。

milkie 每轮已把完整事件溯源 trace 落盘到 `~/.alfred/milkie/<agent>/runs/<runId>.jsonl`。本 skill **只消费** milkie 现成的诊断产物(`milkie trace execution/report`),不重写诊断逻辑。

## 何时用

- 用户要核验某个结论/数字的来源:「你刚那个数怎么来的」「这个结论怎么得到的」「求证一下」
- 用户要看执行过程:「复盘上一轮」「看下你调了什么工具、命中什么」
- 用户排查上一轮为什么慢 / 失败 / 缓存没命中

## 命令

```bash
python skills/trace-review/scripts/review_run.py [选项]
```

| 选项 | 含义 |
|---|---|
| `--agent NAME` | 收窄到指定 agent(缺省扫所有 agent) |
| `--run-id ID` | 显式指定 runId(缺省取最近一个**已完成** run) |
| `--brief` | 精简输出(默认详尽,保留完整 query/命中证据) |
| `--full` | 额外渲染自包含 HTML 报告到 `~/.alfred/logs/traces/<runId>.html` |

缺省行为:取最近一个**已完成**的 run —— 当前正在进行的这一轮尚未完成,会被自动排除,因此缺省即"上一轮"。

### 常见用法

```bash
# 复盘上一轮(全局最近完成的 run)
python skills/trace-review/scripts/review_run.py

# 复盘某个 agent 的上一轮
python skills/trace-review/scripts/review_run.py --agent demo_agent

# 复盘指定 run 并出 HTML
python skills/trace-review/scripts/review_run.py --run-id <runId> --full
```

## 输出契约

返回 markdown:

- **用户问题** / **最终答案**
- **执行步骤**:按序每步 —— LLM(cache 命中率 + region 组成)或 工具(名称 · query · 命中证据 · status)
- **可疑信号**:缓存冷启、工具非零退出 / stderr、空召回等

通过"工具 query + 命中证据 + 最终答案"的对照,即可判断答案里的数据**确实来自某次工具调用的真实返回**(而非编造)。

## 边界

- 只读 milkie trace 产物,不解析 jsonl、不重算 projection(承接 #47 §六)。
- 配置(dist / data-dir-root / node-bin)读 `~/.alfred/config.yaml` 的 `everbot.milkie`,缺省回退默认。
- dist 缺失 / 无已完成 run / CLI 失败 → 返回明确的 markdown 说明,不崩。

## 局限(细粒度血缘需后续工具层增强)

本 skill 做的是**执行过程求证**(看到工具调用与其真实返回)。更细的"某条论断 cite 了哪段原文"的结构化血缘,需要 milkie 侧让取数工具铸 lineage 对象(见 #47 §五讨论),不在本 skill 范围。
