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
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MAX_DESC_CHARS = 150

# 读 SKILL.md 的瞬时失败重试(#81):git checkout / 写入中途文件会短暂不可读,
# 重试一次挡掉绝大多数;仍失败才放弃并 WARNING(绝不静默丢技能)。
_SKILL_MD_READ_RETRIES = 1
_SKILL_MD_RETRY_DELAY_S = 0.05


def _read_skill_md(skill_md: Path) -> Optional[str]:
    """读 SKILL.md 文本;瞬时不可读(git checkout / 写入中途)重试一次。

    彻底失败 → DEBUG + None。这里只记 DEBUG 不 WARNING:此刻还不知该技能是否会被
    更低优先级目录兜住,过早 WARNING 会在 fallback 命中时误报能力丢失。真正的"能力被
    跳过"判定与告警集中在 :func:`discover_skills`(它掌握最终全貌)——这是 #81 的核心:
    去 fail-silent,但把"出声"放在能区分"瞬时/可恢复"与"真丢失"的那一层。
    """
    last_exc: Optional[Exception] = None
    for attempt in range(_SKILL_MD_READ_RETRIES + 1):
        try:
            return skill_md.read_text(encoding="utf-8")
        except Exception as exc:
            last_exc = exc
            if attempt < _SKILL_MD_READ_RETRIES:
                time.sleep(_SKILL_MD_RETRY_DELAY_S)
    logger.debug(
        "SKILL.md 读取失败(重试 %d 次后): %s — [%s] %r",
        _SKILL_MD_READ_RETRIES, skill_md, type(last_exc).__name__, last_exc,
    )
    return None


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
        return None  # 合法:非 skill 目录,静默跳过(避免噪声)
    content = _read_skill_md(skill_md)
    if content is None:
        return None  # 有 SKILL.md 却读失败 —— _read_skill_md 已 WARNING

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


# #81 F2:每个 workspace 上一次成功发现的技能名集合(原始,未过滤),用于"缩水"检测。
# git checkout / 软链接装卸期间,技能目录会整个瞬时消失,F1 的逐文件读重试兜不住
# (目录列举那一刻它就不在)——靠与上次比对发现缩水、重扫整轮骑过瞬时窗口来覆盖。
_DISCOVERY_SHRINK_RETRIES = 2
_last_good_discovery: Dict[str, set] = {}


def _scan_skills_once(workspace_path: Path):
    """扫一遍所有 skill 目录 → ``(技能 meta 列表, 见到 SKILL.md 的目录名集合)``。

    纯单次扫描,不告警/不重试/不碰缓存;供 :func:`_discover_stable` 包装。
    """
    skills: List[Dict[str, Any]] = []
    seen: set[str] = set()
    saw_skill_md: set[str] = set()  # 含 SKILL.md(本应是技能)的目录名
    for skills_dir in resolve_skill_dirs(workspace_path):
        for item in sorted(skills_dir.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            if item.name in seen:
                continue
            if (item / "SKILL.md").exists():
                saw_skill_md.add(item.name)
            meta = parse_skill_metadata(item)
            if meta:
                seen.add(item.name)
                skills.append(meta)
    return skills, saw_skill_md


def _discover_stable(workspace_path: Path):
    """单次扫描 + 缩水重试,返回 ``(skills, saw_skill_md, names)``(均为原始未过滤)。

    与上次成功发现比对:若本次"缩水"(少了曾出现过的技能),疑似目录变更中途,
    重扫整轮(取技能最多的一次)以骑过瞬时窗口;**真删除**会跨重试持续缩水 → 接受
    并 WARNING。无论瞬时与否,缩水都不再被静默吞掉。
    """
    skills, saw = _scan_skills_once(workspace_path)
    names = {s["name"] for s in skills}

    key = str(workspace_path)
    prev = _last_good_discovery.get(key)
    if prev is not None and not prev <= names:
        best, best_saw, best_names = skills, saw, names
        for _ in range(_DISCOVERY_SHRINK_RETRIES):
            time.sleep(_SKILL_MD_RETRY_DELAY_S)
            s2, saw2 = _scan_skills_once(workspace_path)
            n2 = {s["name"] for s in s2}
            if len(n2) > len(best_names):
                best, best_saw, best_names = s2, saw2, n2
            if prev <= n2:
                break
        skills, saw, names = best, best_saw, best_names
        still_missing = prev - names
        if still_missing:
            logger.warning(
                "discover_skills: 技能数较上次缩水(疑似发现期目录变更或确有删除),"
                "重试 %d 次后仍缺: %s",
                _DISCOVERY_SHRINK_RETRIES, sorted(still_missing),
            )

    _last_good_discovery[key] = names
    return skills, saw, names


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
    - ``include`` 里出现**未发现**的 skill 名 → ``ValueError``(fail-loud,allowlist 笔误应暴露);
    - ``exclude`` 里出现**未发现**的 skill 名 → ``WARNING`` + 忽略(#106 E0,不 brick agent;
      exclude 语义="确保不出现",引用不存在者本就满足)。
    """
    skills, saw_skill_md, names = _discover_stable(workspace_path)

    # #81 F1:有 SKILL.md 却没能纳入(且无更低优先级目录兜住)的技能,聚合 WARNING ——
    # 让"发现期目录变更导致静默丢技能"从隐形变可见,不再只有 debug。
    dropped = saw_skill_md - names
    if dropped:
        logger.warning(
            "discover_skills: %d 个技能含 SKILL.md 却未能纳入(读取/解析失败),已被跳过: %s",
            len(dropped), sorted(dropped),
        )

    available = names
    if include:
        unknown = [n for n in include if n not in available]
        if unknown:
            raise ValueError(f"skills.include 含未发现的 skill: {unknown}(可用: {sorted(available)})")
        skills = [s for s in skills if s["name"] in set(include)]
    if exclude:
        # #106 E0:exclude 语义 = "确保这些不出现"。引用一个不存在的 skill 本就已满足该
        # 语义,不该抛错把整个 agent spawn 弄挂(残留/笔误 exclude 不应 brick agent)。
        # → 当作无操作 + WARNING(不静默),与 include(allowlist 笔误 fail-loud)区别对待。
        unknown = [n for n in exclude if n not in available]
        if unknown:
            logger.warning(
                "skills.exclude 含未发现的 skill(已忽略,不影响其余技能): %s(可用: %s)",
                sorted(unknown), sorted(available),
            )
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
        # 列举意图 → 走 skill_list 工具(权威完整),而非凭本段落手抄(手抄是非确定性的,
        # 实测会漏列;skill_list 由 manifest 背书,见 milkie#139 / alfred#50)。
        "⚠️ 当用户要求**列举/罗列你的全部技能**（如「你有哪些技能」「列出技能」）时，"
        "调用 `skill_list` 工具获取权威完整清单后再作答，**不要凭本段落或记忆手动罗列**"
        "（手动罗列易漏列技能）。`skill_list` 返回的每个条目含 `dir` 字段，即上面各技能的目录。",
        "",
    ]
    for s in skills:
        desc = s["description"] or s["title"]
        lines.append(f"- **{s['name']}** — {desc}  [目录: `{s['abs_path']}`]")
    return "\n".join(lines)
