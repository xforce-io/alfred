"""milkie skill 发现单测(#38 E 能力层 alfred 侧)。"""
from pathlib import Path

from src.everbot.core.agent.provider.milkie import skills as msk


def _make_skill(dir_: Path, name: str, title: str, desc: str) -> Path:
    sd = dir_ / name
    sd.mkdir(parents=True)
    (sd / "SKILL.md").write_text(f"# {title}\n\n{desc}\n\n## 用法\n...", encoding="utf-8")
    return sd


def test_parse_skill_metadata_extracts_title_and_description(tmp_path):
    sd = _make_skill(tmp_path, "ops", "Ops 工具", "运维巡检与诊断脚本集合。")
    meta = msk.parse_skill_metadata(sd)
    assert meta is not None
    assert meta["name"] == "ops"
    assert meta["title"] == "Ops 工具"
    assert "运维巡检" in meta["description"]
    assert meta["abs_path"] == str(sd.resolve())


def test_parse_skill_metadata_no_skill_md_returns_none(tmp_path):
    d = tmp_path / "not_a_skill"
    d.mkdir()
    assert msk.parse_skill_metadata(d) is None


def test_parse_skill_metadata_truncates_long_description(tmp_path):
    long_desc = "x" * 500
    sd = _make_skill(tmp_path, "big", "Big", long_desc)
    meta = msk.parse_skill_metadata(sd)
    assert meta["description"].endswith("...")
    assert len(meta["description"]) <= msk._MAX_DESC_CHARS + 3


def test_discover_skills_dedups_with_priority(tmp_path, monkeypatch):
    """同名 skill 高优先级目录(前者)胜出。"""
    hi = tmp_path / "hi"
    lo = tmp_path / "lo"
    _make_skill(hi, "shared", "高优先级版", "workspace 版")
    _make_skill(lo, "shared", "低优先级版", "全局版")
    _make_skill(lo, "only_lo", "仅全局", "只在低优先级目录")

    monkeypatch.setattr(msk, "resolve_skill_dirs", lambda _ws: [hi, lo])
    found = msk.discover_skills(tmp_path)
    by_name = {s["name"]: s for s in found}

    assert set(by_name) == {"shared", "only_lo"}
    assert by_name["shared"]["title"] == "高优先级版"  # hi 胜出
    assert by_name["shared"]["abs_path"] == str((hi / "shared").resolve())


def test_discover_skills_skips_dotdirs_and_non_skill_dirs(tmp_path, monkeypatch):
    d = tmp_path / "d"
    _make_skill(d, "good", "Good", "真 skill")
    (d / ".hidden").mkdir()
    (d / "plain").mkdir()  # 无 SKILL.md
    monkeypatch.setattr(msk, "resolve_skill_dirs", lambda _ws: [d])
    found = msk.discover_skills(tmp_path)
    assert [s["name"] for s in found] == ["good"]


def test_build_section_includes_run_command_and_paths(tmp_path):
    skills = [
        {"name": "ops", "title": "Ops", "description": "运维脚本", "abs_path": "/abs/ops"},
        {"name": "web", "title": "Web", "description": "", "abs_path": "/abs/web"},
    ]
    section = msk.build_milkie_skills_section(skills, Path("/ws"))
    assert "run_command" in section
    assert "ops" in section and "/abs/ops" in section
    # 空 description 退回 title
    assert "Web" in section
    assert "$WORKSPACE_ROOT" in section and "/ws" in section


def test_build_section_nudges_enumeration_to_skill_list(tmp_path):
    """列举意图引导到 skill_list 工具(确定性,防漏列;alfred#50)。"""
    skills = [{"name": "ops", "title": "Ops", "description": "运维", "abs_path": "/abs/ops"}]
    section = msk.build_milkie_skills_section(skills, Path("/ws"))
    assert "skill_list" in section            # 指向工具
    assert "列举" in section or "罗列" in section  # 命中枚举意图
    assert "不要" in section                   # 明确禁止手抄


def test_build_section_empty_when_no_skills():
    # 空技能集仍 ""（不渲染指令，行为不变）。
    assert msk.build_milkie_skills_section([], Path("/ws")) == ""


def _setup_three(tmp_path, monkeypatch):
    d = tmp_path / "d"
    for n in ("alpha", "beta", "gamma"):
        _make_skill(d, n, n.title(), f"{n} skill")
    monkeypatch.setattr(msk, "resolve_skill_dirs", lambda _ws: [d])
    return tmp_path


def test_discover_skills_include_filters_to_allowlist(tmp_path, monkeypatch):
    _setup_three(tmp_path, monkeypatch)
    found = msk.discover_skills(tmp_path, include=["alpha", "beta"])
    assert sorted(s["name"] for s in found) == ["alpha", "beta"]


def test_discover_skills_exclude_removes_listed(tmp_path, monkeypatch):
    _setup_three(tmp_path, monkeypatch)
    found = msk.discover_skills(tmp_path, exclude=["gamma"])
    assert sorted(s["name"] for s in found) == ["alpha", "beta"]


def test_discover_skills_include_then_exclude(tmp_path, monkeypatch):
    _setup_three(tmp_path, monkeypatch)
    found = msk.discover_skills(tmp_path, include=["alpha", "beta"], exclude=["beta"])
    assert [s["name"] for s in found] == ["alpha"]


def test_discover_skills_unknown_in_include_raises(tmp_path, monkeypatch):
    import pytest
    _setup_three(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="nonexistent"):
        msk.discover_skills(tmp_path, include=["alpha", "nonexistent"])


def test_discover_skills_unknown_in_exclude_raises(tmp_path, monkeypatch):
    import pytest
    _setup_three(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="ghost"):
        msk.discover_skills(tmp_path, exclude=["ghost"])


def test_discover_skills_no_filter_returns_all(tmp_path, monkeypatch):
    _setup_three(tmp_path, monkeypatch)
    found = msk.discover_skills(tmp_path)
    assert sorted(s["name"] for s in found) == ["alpha", "beta", "gamma"]


def test_loader_returns_reflect_prompt_for_reflector():
    """#34 C:reflector agent 的 systemPrompt 即 reflect-JSON 提示(不读 workspace)。

    milkie 丢弃 per-turn system_prompt → 自省必须用独立 reflector agent(systemPrompt 即
    reflect 提示)而非业务 agent + override(否则被业务人设污染)。"""
    from src.everbot.core.agent.provider.milkie import provider as mprov

    out = mprov._default_system_prompt_loader(mprov.REFLECTOR_AGENT)
    assert "reflection" in out and "JSON" in out      # 是 reflect 提示
    assert "身份定义" not in out and "已安装技能" not in out  # 不是 workspace 系统提示


def test_loader_applies_per_agent_skill_include(tmp_path, monkeypatch):
    """_default_system_prompt_loader 读 everbot.agents.<name>.skills.include 过滤注入。"""
    from src.everbot.core.agent.provider.milkie import provider as mprov
    import src.everbot.infra.config as config_module

    ws = tmp_path / "ws"
    (ws / "skills").mkdir(parents=True)
    (ws / "SOUL.md").write_text("身份", encoding="utf-8")
    for n in ("ops", "web", "secret"):
        _make_skill(ws / "skills", n, n, f"{n} skill")

    monkeypatch.setattr(mprov, "_resolve_agent_workspace", lambda _n: ws)
    monkeypatch.setattr(msk, "resolve_skill_dirs", lambda _ws: [ws / "skills"])
    monkeypatch.setattr(
        config_module, "get_config",
        lambda *a, **k: {"everbot": {"agents": {"a1": {"skills": {"include": ["ops", "web"]}}}}},
    )
    prompt = mprov._default_system_prompt_loader("a1")
    assert "ops" in prompt and "web" in prompt
    assert "secret" not in prompt  # 不在 include → 不注入


def test_system_prompt_loader_injects_discovered_skills(tmp_path, monkeypatch):
    """_default_system_prompt_loader 把 workspace 指令 + 发现的 skill 都拼进系统提示。"""
    from src.everbot.core.agent.provider.milkie import provider as mprov

    ws = tmp_path / "agent_ws"
    (ws / "skills").mkdir(parents=True)
    (ws / "SOUL.md").write_text("我是测试 agent。", encoding="utf-8")
    _make_skill(ws / "skills", "ops", "Ops", "运维巡检脚本")

    monkeypatch.setattr(mprov, "_resolve_agent_workspace", lambda _n: ws)
    # 隔离真实 ~/.alfred/skills 与仓库 skills/,只看 workspace/skills
    monkeypatch.setattr(msk, "resolve_skill_dirs", lambda _ws: [ws / "skills"])

    prompt = mprov._default_system_prompt_loader("whatever")
    assert "我是测试 agent" in prompt          # workspace 指令在
    assert "ops" in prompt                       # 发现的 skill 在
    assert "run_command" in prompt               # 面向 run_command 的调用说明在


# ── _build_default_prompt_and_skills:同源(prompt + skill_list manifest)─────

def _setup_ws_skills(tmp_path, monkeypatch, names, *, agent="a1", config=None):
    """搭一个 workspace + 若干 skill,隔离全局/仓库 skill 目录。返回 ws。"""
    from src.everbot.core.agent.provider.milkie import provider as mprov
    import src.everbot.infra.config as config_module

    ws = tmp_path / "ws"
    (ws / "skills").mkdir(parents=True)
    (ws / "SOUL.md").write_text("身份", encoding="utf-8")
    for n in names:
        _make_skill(ws / "skills", n, n, f"{n} 的说明")
    monkeypatch.setattr(mprov, "_resolve_agent_workspace", lambda _n: ws)
    monkeypatch.setattr(msk, "resolve_skill_dirs", lambda _ws: [ws / "skills"])
    monkeypatch.setattr(config_module, "get_config", lambda *a, **k: config or {})
    return ws


def test_build_default_returns_prompt_and_skills_same_source(tmp_path, monkeypatch):
    """返回 (prompt, skills);skills 即 manifest 来源,与 prompt 技能段同一份发现结果。"""
    from src.everbot.core.agent.provider.milkie import provider as mprov

    _setup_ws_skills(tmp_path, monkeypatch, ["ops", "web", "twitter-watch"])
    prompt, skills = mprov._build_default_prompt_and_skills("a1")

    names = {s["name"] for s in skills}
    assert names == {"ops", "web", "twitter-watch"}        # 一个不少(防漏列)
    # 同源:返回的 prompt 必须等于薄包装 loader 的输出
    assert prompt == mprov._default_system_prompt_loader("a1")
    # 同源:每个 skill 都出现在 prompt 技能段里
    for n in names:
        assert n in prompt
    # skills 形状含 manifest 需要的 abs_path
    assert all("abs_path" in s and "description" in s for s in skills)


def test_build_default_reflector_returns_none_skills(tmp_path, monkeypatch):
    """reflector:无技能集 → skills is None(调用方据此不产出 manifest)。"""
    from src.everbot.core.agent.provider.milkie import provider as mprov

    prompt, skills = mprov._build_default_prompt_and_skills(mprov.REFLECTOR_AGENT)
    assert skills is None
    assert "reflection" in prompt and "JSON" in prompt


def test_build_default_respects_include_filter(tmp_path, monkeypatch):
    from src.everbot.core.agent.provider.milkie import provider as mprov

    _setup_ws_skills(
        tmp_path, monkeypatch, ["ops", "web", "secret"],
        config={"everbot": {"agents": {"a1": {"skills": {"include": ["ops", "web"]}}}}},
    )
    _prompt, skills = mprov._build_default_prompt_and_skills("a1")
    assert {s["name"] for s in skills} == {"ops", "web"}  # secret 被白名单挡掉


def test_build_default_bad_include_fails_loud(tmp_path, monkeypatch):
    """producer fail-loud(milkie#139):include 引用不存在的 skill → discover_skills raise。"""
    import pytest
    from src.everbot.core.agent.provider.milkie import provider as mprov

    _setup_ws_skills(
        tmp_path, monkeypatch, ["ops"],
        config={"everbot": {"agents": {"a1": {"skills": {"include": ["nope"]}}}}},
    )
    with pytest.raises(ValueError):
        mprov._build_default_prompt_and_skills("a1")


def test_telegram_agent_gets_attachment_instruction(tmp_path, monkeypatch):
    """telegram-serving agent 的系统提示注入附件输出约定;非 telegram agent 不注入。"""
    from src.everbot.core.agent.provider.milkie import provider as mprov
    import src.everbot.infra.config as config_module

    ws = tmp_path / "ws"
    (ws / "skills").mkdir(parents=True)
    (ws / "SOUL.md").write_text("身份", encoding="utf-8")
    monkeypatch.setattr(mprov, "_resolve_agent_workspace", lambda _n: ws)
    monkeypatch.setattr(msk, "resolve_skill_dirs", lambda _ws: [ws / "skills"])
    monkeypatch.setattr(
        config_module, "get_config",
        lambda *a, **k: {"everbot": {"channels": {"telegram": {"enabled": True, "default_agent": "tg_agent"}}}},
    )

    tg_prompt = mprov._default_system_prompt_loader("tg_agent")
    other_prompt = mprov._default_system_prompt_loader("other_agent")
    assert "send_file" in tg_prompt          # telegram agent 有附件约定
    assert "send_file" not in other_prompt   # 非 telegram agent 无


def test_resolve_skill_dirs_orders_workspace_first_and_dedups(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "skills").mkdir(parents=True)
    dirs = msk.resolve_skill_dirs(ws)
    # workspace/skills 必须在最前
    assert dirs[0] == ws / "skills"
    # 无重复
    assert len(dirs) == len(set(str(d) for d in dirs))
