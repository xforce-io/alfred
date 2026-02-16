"""Tests for atomic write + corruption recovery in SessionPersistence."""

import json
import pytest

from src.everbot.core.session.session import SessionData, SessionPersistence


def _make_session_data(session_id: str = "test_session") -> SessionData:
    return SessionData(
        session_id=session_id,
        agent_name="demo_agent",
        model_name="gpt-4",
        history_messages=[{"role": "user", "content": "hello"}],
        variables={"key": "value"},
        created_at="2026-02-11T09:00:00",
        updated_at="2026-02-11T09:00:01",
        timeline=[],
        context_trace={},
    )


class TestAtomicSave:
    """atomic_save writes atomically and creates .bak files."""

    def test_write_creates_file(self, tmp_path):
        target = tmp_path / "data.json"
        data = b'{"hello": "world"}'
        SessionPersistence.atomic_save(target, data)
        assert target.exists()
        assert target.read_bytes() == data

    def test_bak_created_on_second_write(self, tmp_path):
        target = tmp_path / "data.json"
        first = b'{"v": 1}'
        second = b'{"v": 2}'
        SessionPersistence.atomic_save(target, first)
        SessionPersistence.atomic_save(target, second)

        assert target.read_bytes() == second
        bak = target.with_suffix(".json.bak")
        assert bak.exists()
        assert bak.read_bytes() == first

    def test_no_tmp_leftover(self, tmp_path):
        target = tmp_path / "data.json"
        SessionPersistence.atomic_save(target, b'{"ok": true}')
        tmp_file = target.with_suffix(".json.tmp")
        assert not tmp_file.exists()


class TestChecksumIntegrity:
    """Checksum is embedded and verified on load."""

    def test_valid_checksum_round_trip(self, tmp_path):
        sp = SessionPersistence(tmp_path)
        data_dict = {"session_id": "s1", "value": 42}
        serialized = sp._serialize_session(data_dict)
        loaded = sp._validate_and_load_json(serialized)
        assert loaded is not None
        assert loaded["session_id"] == "s1"
        assert loaded["value"] == 42

    def test_tampered_data_fails_checksum(self, tmp_path):
        sp = SessionPersistence(tmp_path)
        data_dict = {"session_id": "s1", "value": 42}
        serialized = sp._serialize_session(data_dict)
        # Tamper with the data (change a character)
        tampered = serialized.replace(b'"value": 42', b'"value": 99')
        loaded = sp._validate_and_load_json(tampered)
        assert loaded is None

    def test_missing_checksum_still_loads(self, tmp_path):
        """Files without _checksum (e.g. legacy) should still load."""
        sp = SessionPersistence(tmp_path)
        raw = json.dumps({"session_id": "legacy", "data": 1}).encode("utf-8")
        loaded = sp._validate_and_load_json(raw)
        assert loaded is not None
        assert loaded["session_id"] == "legacy"

    def test_invalid_json_returns_none(self, tmp_path):
        sp = SessionPersistence(tmp_path)
        loaded = sp._validate_and_load_json(b"not json at all{{{")
        assert loaded is None


class TestCorruptionRecovery:
    """SessionPersistence.load falls back to .bak on corruption."""

    @pytest.mark.asyncio
    async def test_loads_main_file_normally(self, tmp_path):
        sp = SessionPersistence(tmp_path)
        sd = _make_session_data()
        await sp.save_data(sd)

        loaded = await sp.load("test_session")
        assert loaded is not None
        assert loaded.session_id == "test_session"

    @pytest.mark.asyncio
    async def test_falls_back_to_bak_on_corrupt_main(self, tmp_path):
        sp = SessionPersistence(tmp_path)
        sd = _make_session_data()
        await sp.save_data(sd)

        # Corrupt the main file
        main_path = tmp_path / "test_session.json"
        main_path.write_text("CORRUPTED DATA{{{", encoding="utf-8")

        # Create a valid .bak
        sd2 = _make_session_data()
        sd2.model_name = "gpt-3.5"
        bak_data = sp._serialize_session(sd2.to_dict())
        bak_path = main_path.with_suffix(".json.bak")
        bak_path.write_bytes(bak_data)

        loaded = await sp.load("test_session")
        assert loaded is not None
        assert loaded.model_name == "gpt-3.5"

    @pytest.mark.asyncio
    async def test_returns_none_when_both_corrupt(self, tmp_path):
        sp = SessionPersistence(tmp_path)
        main_path = tmp_path / "test_session.json"
        main_path.write_text("BAD", encoding="utf-8")
        bak_path = main_path.with_suffix(".json.bak")
        bak_path.write_text("ALSO BAD", encoding="utf-8")

        loaded = await sp.load("test_session")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_returns_none_when_file_missing(self, tmp_path):
        sp = SessionPersistence(tmp_path)
        loaded = await sp.load("nonexistent")
        assert loaded is None

    @pytest.mark.asyncio
    async def test_save_then_save_creates_bak(self, tmp_path):
        """Two consecutive saves should leave a .bak from the first."""
        sp = SessionPersistence(tmp_path)
        sd1 = _make_session_data()
        sd1.model_name = "v1"
        await sp.save_data(sd1)

        sd2 = _make_session_data()
        sd2.model_name = "v2"
        await sp.save_data(sd2)

        bak_path = (tmp_path / "test_session.json").with_suffix(".json.bak")
        assert bak_path.exists()

        loaded = await sp.load("test_session")
        assert loaded.model_name == "v2"
