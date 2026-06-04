"""milkie 侧 skill 发现 —— 把磁盘上的 shell 型 markdown skill 注入 milkie agent 提示。

dolphin 靠 ResourceSkillkit 扫目录发现 SKILL.md + `_load_resource_skill` 工具加载;
milkie 没有这套(skill_list 是空 stub)。本模块在 alfred 侧(skill 目录所在地)做发现,
渲染成一段「可用技能」提示,让 milkie agent 用内建 ``run_command``(milkie#134)读
SKILL.md 并执行其脚本 —— 与 dolphin 的能力对等,但不耦合 dolphin。

目录优先级同 dolphin factory:``<workspace>/skills`` > ``~/.alfred/skills`` > 仓库内置
``skills/``。同名 skill 高优先级目录胜出。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_DESC_CHARS = 150


def resolve_skill_dirs(workspace_path: Path) -> List[Path]:
    """按优先级返回存在的 skill 目录(高优先级在前)。"""
    candidates = [
        Path(workspace_path) / "skills",
        Path("~/.alfred/skills").expanduser(),
        # repo root = …/alfred;本文件在 …/alfred/src/everbot/core/agent/provider/milkie/
        Path(__file__).resolve().parents[6] / "skills",
    ]
    dirs: List[Path] = []
    seen: set[str] = set()
    for d in candidates:
        key = str(d)
        if key in seen:
            continue
        seen.add(key)
        if d.exists() and d.is_dir():
            dirs.append(d)
    return dirs


def parse_skill_metadata(skill_dir: Path) -> Optional[Dict[str, Any]]:
    """解析 SKILL.md 取元数据(name/title/description/abs_path)。无 SKILL.md → None。"""
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None
    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception:
        logger.debug("读取 SKILL.md 失败: %s", skill_md, exc_info=True)
        return None

    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else skill_dir.name

    # 描述 = 标题行之后的第一个非空段落(到空行或下一个标题为止)。逐行扫,
    # 避免 DOTALL 正则贪婪跨行误吞后续小标题。
    after = content[title_match.end():] if title_match else content
    para: List[str] = []
    for line in after.splitlines():
        stripped = line.strip()
        if not para:
            if not stripped:
                continue  # 跳过标题后的前导空行
            if stripped.startswith("#"):
                break  # 标题后紧跟另一个标题 → 无描述
        elif not stripped or stripped.startswith("#"):
            break  # 段落结束
        para.append(stripped)
    description = " ".join(para).strip()
    if len(description) > _MAX_DESC_CHARS:
        description = description[:_MAX_DESC_CHARS] + "..."

    return {
        "name": skill_dir.name,
        "title": title,
        "description": description,
        "abs_path": str(skill_dir.resolve()),
    }


def discover_skills(
    workspace_path: Path,
    *,
    include: Optional[List[str]] = None,
    exclude: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """扫所有 skill 目录,返回去重后的可用 skill 元数据列表(高优先级目录胜出)。

    per-agent allowlist(对应 dolphin 的 ``everbot.agents.<name>.skills.include/exclude``):
    - ``include`` 非空 → 只保留其中的 skill;
    - ``exclude`` → 移除其中的 skill;
    - include/exclude 里出现**未发现**的 skill 名 → ``ValueError``(fail-loud,防配置笔误,
      与 dolphin 行为一致)。
    """
    skills: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for skills_dir in resolve_skill_dirs(workspace_path):
        for item in sorted(skills_dir.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            if item.name in seen:
                continue
            meta = parse_skill_metadata(item)
            if meta:
                seen.add(item.name)
                skills.append(meta)

    available = {s["name"] for s in skills}
    if include:
        unknown = [n for n in include if n not in available]
        if unknown:
            raise ValueError(f"skills.include 含未发现的 skill: {unknown}(可用: {sorted(available)})")
        skills = [s for s in skills if s["name"] in set(include)]
    if exclude:
        unknown = [n for n in exclude if n not in available]
        if unknown:
            raise ValueError(f"skills.exclude 含未发现的 skill: {unknown}(可用: {sorted(available)})")
        skills = [s for s in skills if s["name"] not in set(exclude)]
    return skills


def build_milkie_skills_section(skills: List[Dict[str, Any]], workspace_root: Path) -> str:
    """渲染面向 ``run_command`` 的「可用技能」提示段。空列表 → ""。"""
    if not skills:
        return ""
    lines = [
        "# 已安装技能（用 run_command 调用）",
        "",
        "下列技能可用。使用方法：用 `run_command` 执行 `cat \"<技能目录>/SKILL.md\"` 阅读其文档，"
        "再按文档用 `run_command` 执行其脚本（用脚本的绝对路径）。文档里的 "
        f"`$SKILL_DIR` 指该技能目录、`$WORKSPACE_ROOT` 指 `{workspace_root}`。",
        "",
    ]
    for s in skills:
        desc = s["description"] or s["title"]
        lines.append(f"- **{s['name']}** — {desc}  [目录: `{s['abs_path']}`]")
    return "\n".join(lines)
