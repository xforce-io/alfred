# 124 — 报告型技能可信度:逐条结论可溯源

> Issue: #124 · 关联: #122(L0 投递溯源)· 概念基础: milkie `docs/zh/concepts-observability-vs-lineage.md`

## 1. 问题:不是一个 bug,是一个问题类

排查 #122(投递溯源断链)时挖出的根本问题,不限于灰犀牛:

> alfred 的"报告型技能"普遍以不透明 python 子进程产出聚合结论;**既不强制工具真执行**(可以不跑就编),**也不把原始证据铸成原子 object 进 milkie 血缘图**(可观测只到"整坨输出",逐条结论无法机械溯源)。

三个叠加失败模式:

| # | 失败模式 | 根因 | 用户体感 |
|---|---|---|---|
| F1 | 投递溯源断链 | 投递锚点用合成 job 号 | "这报告哪次跑的"——查无此物 |
| F2 | 不跑就编整份报告 | "NEVER fabricate" 只是提示词,未强制 | 数字像凭空捏 |
| F3 | 真跑了也说不清细节来源 | 原始条目(title+source+url)在聚类/落盘丢失;tool 输出是整坨 blob,逐条结论无原子源 | 问"这条哪来的"→ 含糊或现编 |

## 2. 范围:所有"抓数据→聚合→出报告"的 routine 同病

灰犀牛 `routine_d4112f2e` · 每日投资信号 `routine_38364fe6` · 每日论文 `routine_476c4861` · Serenity分析 `routine_3d785e79` · 每日新闻简报 `routine_86e85e59` · `contrarian-signals`…

共用同一管道形状 → 共用同一组失败。**解法在共性层,不逐技能打补丁。**

## 3. 分层解法

坐标轴见 milkie 概念文档「可观测 vs 血缘」。

| 层 | 做什么 | 堵住 | 成本 | 通用性 | 本 issue |
|---|---|---|---|---|---|
| **L0 可观测底座** | 投递可溯源到产出 run(`projection_source_id`) | F1 | — | 框架级 | #122 已完成 |
| **L1 强制工具执行** | 报告的数必须有真实 tool 调用支撑,否则不准发 | F2 | 中 | 框架级闸门 | 🔜 后续 |
| **L2 粗粒度血缘** | 每条结论 `cite` 到那次抓取/报告的 object;且该 object 内含逐条证据 | F2 + 半个 F3 | 低 | 约定 + 共享流程 | ✅ **本 issue 灰犀牛样板** |
| **L3 原子血缘** | 数据工具逐条铸 object(url 进 meta),结论 `derives_from` 具体条目 | 整个 F3 | 高 | 需共享"数据工具"层 | ⛔ 暂不做 |

**关键认识**:L2 = **血缘的边 + 可观测的节点**——堵"凭空编源"(`resolveObject` 拒伪造 objectId),堵不住"真源里误引"(节点是整坨 blob,「数 ∈ blob」无人机械校验)。L3 把节点原子化才两个都堵。**python/TS 边界在 L3 才真正咬人**(逐条铸 object 须在 TS 侧,python 子进程碰不到 `registerObject`),故本 issue 不碰 L3。

## 4. 本 issue 落地:灰犀牛 L2 样板

L2 要"可用",cite 指向的节点必须真带证据。当前 `rhino_analyzer.py` 建 cluster 时丢掉了每条新闻的 `url`(只留 `sources` 源名 + `representative_title`)。所以样板分两步:

### 4.1 止损 F3 的数据丢失(本 issue 实现,TDD)

- `rhino_analyzer.NewsCluster` 增 `evidence` 字段:每个 cluster 的贡献条目 `[{title, source, url, published}]`,在建 cluster 时从 `group_items` 填入(url 本就在 group_items 里)。
- `rhino_report.py` 逐条信号输出 `evidence`:
  - `--format json`:每个 signal 带完整 `evidence[]`。
  - `--format text`:每条信号尾部列 top-N 条「标题 — 链接」(紧凑,定时推送带得动)。
- 这样被 cite 的报告 object **内含逐条来源链接**,L2 的粗粒度 cite 才真正可用,也为 L3 备好数据。

### 4.2 L2 cite 约定(本 issue:写进 SKILL.md)

- gray-rhino `SKILL.md` 增约定:出报告后,**每条信号 `cite` 到报告/抓取的 object**(milkie shell 工具已自动把 stdout 铸成 `shell:stdout` object 并在结果带回 `objectId`)。
- 效果:`get_lineage(query="某信号")` 走 cite 图返回报告 object;`resolveObject` 保证来源不可伪造。
- **诚实标注边界**:这是**报告级**血缘(粗粒度)。"具体哪篇文章"仍需打开 object 看(4.1 的 evidence 让这一步有据可依),机械的逐条绑定属 L3。

### 4.3 不做(明确排除)

- L3 原子 object / `derives_from` 逐条边(需 TS 数据工具层,另立项)。
- L1 强制执行闸门(需框架级"未引用结论"检测)。
- 把 L2 铺到其余 routine(灰犀牛跑通后再铺)。

## 5. 共性杠杆(后续,非本 issue)

不逐技能改提示词,而建可复用基座:

1. **共享"可引用数据工具"层**(L3 通用版):milkie 原生 `fetch_*`/`query_*` 逐条 `citeable`,所有报告型技能的抓取走它 → 逐条 object 免费。
2. **报告型技能作者约定 + 模板**:数据经可引用工具产出 + 每条结论 cite。
3. **"未引用结论"闸门**(L1):投递前检测带数字/事实的结论是否有 cites 边,否则拦下或标降级。

## 6. 验收

- TDD:`rhino_report --format json` 每条 signal 的 `evidence` 非空、url 来自真实抓取条目;analyzer 保留 per-item url。
- 端到端:重跑灰犀牛,报告逐条带来源链接;SKILL.md cite 约定就位。
