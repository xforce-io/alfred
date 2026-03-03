#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json

# 定义任务数据
tasks_data = {
    "version": 2,
    "tasks": [
        {
            "id": "routine_86e85e59",
            "title": "每日新闻简报生成",
            "description": "生成每日新闻简报，汇总重要新闻",
            "source": "manual",
            "enabled": True,
            "schedule": "1d",
            "timezone": "Asia/Shanghai",
            "execution_mode": "isolated",
            "state": "pending",
            "last_run_at": "2026-02-28T09:08:44.145314",
            "next_run_at": "2026-03-01T09:08:44.145314+08:00",
            "timeout_seconds": 300,
            "retry": 0,
            "max_retry": 3,
            "error_message": None,
            "created_at": "2026-02-14T15:26:17.517527+00:00"
        },
        {
            "id": "routine_476c4861",
            "title": "每日论文报告生成",
            "description": "使用 paper-discovery 技能获取论文。执行步骤:\\n1. 加载技能: _load_resource_skill(\"paper-discovery\")\\n2. 获取论文数据: python skills/paper-discovery/scripts/fetch_papers.py --source both --limit 10 --format json\\n3. 用 JSON 数据生成格式化报告\\n\\n报告格式要求:\\n📚 今日 AI 论文热榜 (YYYY-MM-DD)\\n\\n每篇论文格式:\\n{序号}. {🔥重复heat_level次} {title}\\n   👍 {upvotes} upvotes | ⭐ {github_stars} GitHub stars (如有)\\n   🤖 {ai_summary} (优先使用ai_summary，如无则用abstract前200字)\\n   🏷️ Keywords: {ai_keywords}\\n   🔗 [arXiv]({arxiv_url}) | [PDF]({pdf_url}) | [HuggingFace]({hf_url}){ GitHub|{github_repo}}\\n\\n文末统计:\\n- 总论文数: {N} 篇 (HuggingFace: {N} | arXiv AI: {N} | arXiv ML: {N})\\n- 高热度论文 (≥60分): {N} 篇\\n- 有开源代码: {N} 篇\\n\\n重要提示:\\n- 必须使用 --format json 获取完整结构化数据\\n- 必须显示 heat_level 对应的 🔥 emoji (1-5个)\\n- 优先展示 ai_summary，它比 abstract 更精炼\\n- 显示 github_stars 如果有开源实现\\n- 保留所有链接可点击",
            "source": "manual",
            "enabled": True,
            "schedule": "1d",
            "timezone": "Asia/Shanghai",
            "execution_mode": "isolated",
            "state": "pending",
            "last_run_at": "2026-02-28T00:15:18.471503",
            "next_run_at": "2026-03-01T00:15:18.471503+08:00",
            "timeout_seconds": 300,
            "retry": 0,
            "max_retry": 3,
            "error_message": None,
            "created_at": "2026-02-14T15:26:18.688890+00:00"
        },
        {
            "id": "routine_38364fe6",
            "title": "每日投资信号推送",
            "description": "每天中午12:00运行投资信号检测，整合宏观流动性监控和美股价值投资分析，推送综合信号报告",
            "source": "manual",
            "enabled": True,
            "schedule": "1d",
            "timezone": "Asia/Shanghai",
            "execution_mode": "isolated",
            "state": "running",
            "last_run_at": "2026-02-28T04:11:03.398710+00:00",
            "next_run_at": "2026-02-28T12:11:02.694151+08:00",
            "timeout_seconds": 600,
            "retry": 0,
            "max_retry": 3,
            "error_message": None,
            "created_at": "2026-02-22T15:49:40.602521+00:00"
        },
        {
            "id": "routine_7dcfa7a9",
            "title": "每日投资吸引子推送",
            "description": "每日15:00推送吸引子变化案例，追踪估值锚点迁移。监控全球市场资产，识别边际定价者切换、驱动维度变化的案例。",
            "source": "manual",
            "enabled": True,
            "schedule": "0 15 * * *",
            "timezone": "Asia/Shanghai",
            "execution_mode": "isolated",
            "state": "pending",
            "last_run_at": None,
            "next_run_at": "2026-02-28T15:00:00+08:00",
            "timeout_seconds": 120,
            "retry": 0,
            "max_retry": 3,
            "error_message": None,
            "created_at": "2026-02-27T03:02:22.436188+00:00"
        }
    ]
}

# 使用 json.dumps 生成正确的 JSON 字符串
json_content = json.dumps(tasks_data, ensure_ascii=False, indent=2)

# 验证生成的 JSON
data = json.loads(json_content)
print(f"JSON 生成成功，包含 {len(data['tasks'])} 个任务")

# 生成完整的 HEARTBEAT.md 内容
heartbeat_content = f"""# HEARTBEAT

## Tasks

```json
{json_content}
```
"""

# 写入文件
with open('HEARTBEAT.md', 'w', encoding='utf-8', newline='\n') as f:
    f.write(heartbeat_content)

print("HEARTBEAT.md 已完全重建")

# 验证文件内容
with open('HEARTBEAT.md', 'r', encoding='utf-8') as f:
    verify_content = f.read()

import re
verify_match = re.search(r'```json\n(.*?)\n```', verify_content, re.DOTALL)
if verify_match:
    verify_json = verify_match.group(1)
    verify_data = json.loads(verify_json)
    print(f"文件验证成功，包含 {len(verify_data['tasks'])} 个任务")
    for t in verify_data['tasks']:
        print(f"  - {t['id']}: {t['title']}")
