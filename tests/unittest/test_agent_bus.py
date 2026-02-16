"""Tests for agent-scoped event broadcasting via events.py."""

import asyncio
import pytest

from src.everbot.core.runtime import events


@pytest.fixture(autouse=True)
def _clean_subscribers():
    """Ensure subscriber list is clean before/after each test."""
    events._subscribers.clear()
    yield
    events._subscribers.clear()


class TestEmitEnvelope:
    """emit() enriches data with envelope fields."""

    @pytest.mark.asyncio
    async def test_default_scope_is_session(self):
        received = []

        async def handler(sid, data):
            received.append(data)

        events.subscribe(handler)
        await events.emit("sess1", {"type": "status", "content": "hi"})

        assert len(received) == 1
        assert received[0]["scope"] == "session"
        assert received[0]["session_id"] == "sess1"
        assert "event_id" in received[0]
        assert "timestamp" in received[0]
        assert received[0]["schema"] == "everbot.event"
        assert received[0]["schema_version"] == 1
        assert received[0]["deliver"] is True

    @pytest.mark.asyncio
    async def test_agent_scope_emitted(self):
        received = []

        async def handler(sid, data):
            received.append(data)

        events.subscribe(handler)
        await events.emit(
            "sess1", {"type": "status"},
            agent_name="bot1", scope="agent",
            source_type="heartbeat", run_id="run_123",
        )

        assert len(received) == 1
        d = received[0]
        assert d["scope"] == "agent"
        assert d["agent_name"] == "bot1"
        assert d["source_type"] == "heartbeat"
        assert d["run_id"] == "run_123"
        assert d["schema"] == "everbot.event"
        assert d["schema_version"] == 1

    @pytest.mark.asyncio
    async def test_no_subscribers_is_noop(self):
        # Should not raise
        await events.emit("s", {"type": "test"})

    @pytest.mark.asyncio
    async def test_subscriber_exception_does_not_propagate(self):
        def bad_handler(sid, data):
            raise RuntimeError("boom")

        events.subscribe(bad_handler)
        # Should not raise
        await events.emit("s", {"type": "test"})

    @pytest.mark.asyncio
    async def test_backward_compatible_no_kwargs(self):
        """Callers without keyword args still work (defaults)."""
        received = []

        async def handler(sid, data):
            received.append(data)

        events.subscribe(handler)
        await events.emit("s1", {"type": "ping"})

        d = received[0]
        assert d["scope"] == "session"
        assert "agent_name" not in d
        assert "source_type" not in d

    @pytest.mark.asyncio
    async def test_deliver_flag_preserves_explicit_false(self):
        received = []

        async def handler(sid, data):
            received.append(data)

        events.subscribe(handler)
        await events.emit("s1", {"type": "status", "deliver": False})

        assert len(received) == 1
        assert received[0]["deliver"] is False


class TestChatServiceRouting:
    """Test ChatService connection index and routing logic.

    These are unit-level tests using mock WebSockets, not full integration.
    """

    def _make_mock_ws(self):
        """Create a simple mock WebSocket."""
        class MockWS:
            def __init__(self):
                self.sent = []
            async def send_json(self, data):
                self.sent.append(data)
        return MockWS()

    def test_register_and_unregister(self):
        """_register_connection / _unregister_connection maintain indices."""
        from src.everbot.web.services.chat_service import ChatService
        svc = ChatService.__new__(ChatService)
        # Init the class-level dicts on this instance
        svc._active_connections = {}
        svc._connections_by_agent = {}
        svc._last_activity = {}
        svc._last_agent_broadcast = {}

        ws1 = self._make_mock_ws()
        ws2 = self._make_mock_ws()

        svc._register_connection("sess_a", "bot1", ws1)
        svc._register_connection("sess_b", "bot1", ws2)

        assert svc._active_connections["sess_a"] is ws1
        assert svc._active_connections["sess_b"] is ws2
        assert len(svc._connections_by_agent["bot1"]) == 2

        svc._unregister_connection("sess_a", "bot1")
        assert "sess_a" not in svc._active_connections
        assert len(svc._connections_by_agent["bot1"]) == 1

        svc._unregister_connection("sess_b", "bot1")
        assert "bot1" not in svc._connections_by_agent

    @pytest.mark.asyncio
    async def test_agent_scope_broadcasts_to_all_agent_connections(self):
        """scope=agent should send to all connections of the same agent."""
        from src.everbot.web.services.chat_service import ChatService
        svc = ChatService.__new__(ChatService)
        svc._active_connections = {}
        svc._connections_by_agent = {}
        svc._last_activity = {}
        svc._last_agent_broadcast = {}

        ws1 = self._make_mock_ws()
        ws2 = self._make_mock_ws()
        svc._register_connection("sess_a", "bot1", ws1)
        svc._register_connection("sess_b", "bot1", ws2)

        data = {
            "type": "status",
            "scope": "agent",
            "agent_name": "bot1",
            "content": "heartbeat running",
        }
        await svc._on_background_event("sess_a", data)

        assert len(ws1.sent) == 1
        assert len(ws2.sent) == 1
        assert ws1.sent[0]["content"] == "heartbeat running"

    @pytest.mark.asyncio
    async def test_session_scope_only_sends_to_matching_session(self):
        """scope=session should only send to the matching session_id."""
        from src.everbot.web.services.chat_service import ChatService
        svc = ChatService.__new__(ChatService)
        svc._active_connections = {}
        svc._connections_by_agent = {}
        svc._last_activity = {}
        svc._last_agent_broadcast = {}

        ws1 = self._make_mock_ws()
        ws2 = self._make_mock_ws()
        svc._register_connection("sess_a", "bot1", ws1)
        svc._register_connection("sess_b", "bot1", ws2)

        data = {
            "type": "delta",
            "scope": "session",
            "content": "user reply",
        }
        await svc._on_background_event("sess_a", data)

        assert len(ws1.sent) == 1
        assert len(ws2.sent) == 0
