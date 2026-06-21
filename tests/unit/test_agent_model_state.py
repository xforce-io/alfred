"""TDD #93 件B:从运行 sidecar 的 agent.md 读「生效模型」,与配置目标比对出 stale。

回应 #91 痛点:改完 models.yaml 后,没有命令能告诉你 agent 此刻实际在用什么模型、
是否需要重启才生效。
"""
from pathlib import Path

from src.everbot.cli.agent_model_state import (
    parse_agent_md_model,
    collect_agent_model_states,
)

_AGENT_MD = """\
@_date() -> date

model:
  provider: volcengine
  model: glm-5.2
  adapter: openai-compatible
  baseUrl: https://ark.cn-beijing.volces.com/api/coding/v3
models:
  fast:
    provider: volcengine
    model: doubao-seed-2-0-pro-260215
    adapter: openai-compatible
---
some prose here, model: not-this
"""


def test_parse_agent_md_model_reads_primary_not_fast(tmp_path):
    p = tmp_path / "agent.md"
    p.write_text(_AGENT_MD, encoding="utf-8")
    assert parse_agent_md_model(p) == "glm-5.2"  # 主模型,非 fast 档的 doubao


def test_parse_agent_md_model_missing_file(tmp_path):
    assert parse_agent_md_model(tmp_path / "nope.md") is None


def test_collect_states_marks_stale_when_effective_differs(tmp_path):
    milkie_root = tmp_path / "milkie"
    (milkie_root / "demo").mkdir(parents=True)
    (milkie_root / "demo" / "agent.md").write_text(_AGENT_MD, encoding="utf-8")

    # 配置目标是 doubao,但运行 agent.md 是 glm-5.2 → stale(待重启)
    states = collect_agent_model_states(
        ["demo"],
        milkie_root=milkie_root,
        configured_resolver=lambda a: "doubao-seed-2-0-pro-260215",
    )
    s = states[0]
    assert s["agent"] == "demo"
    assert s["effective"] == "glm-5.2"
    assert s["configured"] == "doubao-seed-2-0-pro-260215"
    assert s["stale"] is True


def test_collect_states_not_stale_when_match(tmp_path):
    milkie_root = tmp_path / "milkie"
    (milkie_root / "demo").mkdir(parents=True)
    (milkie_root / "demo" / "agent.md").write_text(_AGENT_MD, encoding="utf-8")
    states = collect_agent_model_states(
        ["demo"], milkie_root=milkie_root, configured_resolver=lambda a: "glm-5.2"
    )
    assert states[0]["stale"] is False


def test_collect_states_no_sidecar_when_agent_md_absent(tmp_path):
    milkie_root = tmp_path / "milkie"
    milkie_root.mkdir()
    states = collect_agent_model_states(
        ["demo"], milkie_root=milkie_root, configured_resolver=lambda a: "glm-5.2"
    )
    s = states[0]
    assert s["effective"] is None
    assert s["stale"] is False  # 无运行 sidecar → 不判 stale(还没生效一说)
