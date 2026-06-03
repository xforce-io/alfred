# tests/unit/test_create_path_routing.py

from src.everbot.core.agent.agent_service import AgentService


async def test_create_agent_instance_routes_through_provider(monkeypatch, tmp_path):
    called = {}

    class _FakeProvider:
        async def create_agent(self, name, workspace_path, **kw):
            called["name"] = name
            called["ws"] = workspace_path
            return f"agent::{name}"

    import src.everbot.core.agent.agent_service as svc
    monkeypatch.setattr(svc, "get_provider_for_agent", lambda name: _FakeProvider())

    service = AgentService()
    agent_dir = tmp_path / "alice"
    agent_dir.mkdir()
    monkeypatch.setattr(service.user_data, "get_agent_dir", lambda n: agent_dir)
    monkeypatch.setattr(svc, "ensure_continue_chat_compatibility", lambda: None)

    agent = await service.create_agent_instance("alice")
    assert agent == "agent::alice"
    assert called["name"] == "alice"
    assert called["ws"] == agent_dir
