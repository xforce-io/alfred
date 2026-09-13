"""Deny-by-default access control (#227): agent-name chokepoint, config
validation, doctor checks and the channel-facing error payload."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from src.everbot.cli.doctor import check_access_config
from src.everbot.infra.config import _validate_config, expand_env_refs
from src.everbot.infra.user_data import UserDataManager, is_valid_agent_name


# ---------------------------------------------------------------------------
# S5: agent name chokepoint
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["demo_agent", "a", "A-1_b", "x" * 64])
def test_valid_agent_names(name):
    assert is_valid_agent_name(name)


@pytest.mark.parametrize("name", ["", "../..", "a/b", "a\\b", ".", "..", "a b", "x" * 65, "名字", None])
def test_invalid_agent_names(name):
    assert not is_valid_agent_name(name)


def test_get_agent_dir_rejects_traversal(tmp_path):
    udm = UserDataManager(alfred_home=tmp_path)
    with pytest.raises(ValueError, match="Invalid agent name"):
        udm.get_agent_dir("../..")
    with pytest.raises(ValueError):
        udm.get_agent_tmp_dir("../etc")
    assert udm.get_agent_dir("demo_agent") == tmp_path / "agents" / "demo_agent"


# ---------------------------------------------------------------------------
# Config contract
# ---------------------------------------------------------------------------

def test_validate_config_accepts_new_fields():
    _validate_config({"everbot": {
        "agents": {"demo": {"env_passthrough": ["TAVILY_API_KEY"]}},
        "channels": {"telegram": [{"allow_all": False, "allowed_chat_ids": ["1"]}]},
    }})
    _validate_config({"everbot": {"channels": {"telegram": {"allow_all": True}}}})


@pytest.mark.parametrize("cfg, msg", [
    ({"agents": {"demo": {"env_passthrough": "TAVILY_API_KEY"}}}, "env_passthrough"),
    ({"agents": {"demo": {"env_passthrough": ["", "X"]}}}, "env_passthrough"),
    ({"channels": {"telegram": [{"allow_all": "yes"}]}}, "allow_all"),
    ({"channels": {"telegram": [{"allowed_chat_ids": "123"}]}}, "allowed_chat_ids"),
])
def test_validate_config_rejects_bad_types(cfg, msg):
    with pytest.raises(ValueError, match=msg):
        _validate_config({"everbot": cfg})


# ---------------------------------------------------------------------------
# S6: doctor surfaces the three deny-by-default settings
# ---------------------------------------------------------------------------

def _levels(items, title_prefix):
    return [i.level for i in items if i.title.startswith(title_prefix)]


def test_doctor_flags_missing_everything():
    items = check_access_config(
        {"channels": {"telegram": [{"name": "alfred"}]},
         "web": {},
         "agents": {"demo": {"env_passthrough": ["TAVILY_API_KEY", "TUSHARE_TOKEN"]}}},
        environ={"TAVILY_API_KEY": "x"},
    )
    assert _levels(items, "Telegram access (alfred)") == ["ERROR"]
    assert _levels(items, "Web api_key") == ["ERROR"]
    env_items = [i for i in items if i.title.startswith("env_passthrough (demo)")]
    assert len(env_items) == 1 and env_items[0].level == "WARN"
    assert "TUSHARE_TOKEN" in env_items[0].details and "TAVILY_API_KEY" not in env_items[0].details


def test_doctor_warns_on_allow_all_and_unresolved_api_key_ref():
    items = check_access_config(
        {"channels": {"telegram": {"allow_all": True}}, "web": {"api_key": "${EVERBOT_WEB_API_KEY}"}},
        environ={},
    )
    assert _levels(items, "Telegram access") == ["WARN"]
    key_items = [i for i in items if i.title == "Web api_key"]
    assert len(key_items) == 1 and key_items[0].level == "ERROR"
    assert "EVERBOT_WEB_API_KEY" in key_items[0].details


def test_doctor_is_quiet_when_configured():
    items = check_access_config(
        {"channels": {"telegram": [{"allowed_chat_ids": ["1"]}]},
         "web": {"api_key": "${EVERBOT_WEB_API_KEY}"},
         "agents": {"demo": {"env_passthrough": ["HOME"]}}},
        environ={"EVERBOT_WEB_API_KEY": "k", "HOME": "/h"},
    )
    assert items == []


# ---------------------------------------------------------------------------
# ${ENV} expansion is strict
# ---------------------------------------------------------------------------

def test_expand_env_refs_is_strict():
    assert expand_env_refs("pre-${A}-${B}", environ={"A": "1", "B": "2"}) == "pre-1-2"
    assert expand_env_refs("literal", environ={}) == "literal"
    with pytest.raises(ValueError, match="MISSING_KEY"):
        expand_env_refs("${MISSING_KEY}", environ={})


# ---------------------------------------------------------------------------
# S3: channel error payload carries no traceback
# ---------------------------------------------------------------------------

def test_channel_error_payload_has_only_reference():
    from tests.unit.test_channel_core_service import (
        _EventCollector, _FailingAgent, _make_core_service,
    )

    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            core = _make_core_service(Path(tmp))
            collector = _EventCollector()
            await core.process_message(
                _FailingAgent(), "demo_agent", "web_session_demo_agent", "hi", collector,
            )
            return collector

    collector = asyncio.run(run())
    errors = collector.payloads_by_type("error")
    assert len(errors) == 1
    text = errors[0].content
    assert text.startswith("执行失败（ref=")
    for leak in ("Traceback", 'File "', "Exception", "\n"):
        assert leak not in text
