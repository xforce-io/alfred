"""Unit tests for skill import whitelist validation in CronExecutor and HeartbeatRunner."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.cron import ALLOWED_SKILLS, CronExecutor
from src.everbot.core.runtime.cron_delivery import CronDelivery
from src.everbot.core.tasks.routine_manager import RoutineManager


def _make_executor(tmp_path: Path) -> CronExecutor:
    sm = AsyncMock()
    sm.get_primary_session_id.return_value = "web_session_test"
    sm.get_heartbeat_session_id.return_value = "heartbeat_session_test"
    delivery = CronDelivery(
        session_manager=sm,
        primary_session_id="web_session_test",
        heartbeat_session_id="heartbeat_session_test",
        agent_name="test_agent",
        realtime_push=False,
    )
    return CronExecutor(
        agent_name="test_agent",
        workspace_path=tmp_path,
        session_manager=sm,
        agent_factory=AsyncMock(),
        routine_manager=RoutineManager(tmp_path),
        delivery=delivery,
    )


class TestAllowedSkillsWhitelist:
    """Verify the ALLOWED_SKILLS constant is well-formed."""

    def test_contains_expected_skills(self):
        assert "health_check" in ALLOWED_SKILLS
        assert "memory_review" in ALLOWED_SKILLS
        assert "task_discover" in ALLOWED_SKILLS

    def test_is_frozenset(self):
        assert isinstance(ALLOWED_SKILLS, frozenset)


class TestCronSkillWhitelist:
    """CronExecutor._invoke_skill rejects skills not in the whitelist."""

    def test_allowed_skill_is_imported(self, tmp_path):
        executor = _make_executor(tmp_path)
        task = MagicMock()
        task.skill = "health_check"

        mock_module = MagicMock()
        mock_module.run = AsyncMock(return_value="ok")

        with patch.object(executor, "_build_skill_context", return_value=MagicMock()), \
             patch("importlib.import_module", return_value=mock_module) as mock_import:
            result = asyncio.get_event_loop().run_until_complete(
                executor._invoke_skill(task, None, "run-1")
            )
            mock_import.assert_called_once_with("everbot.core.jobs.health_check")
            assert result == "ok"

    def test_hyphenated_skill_normalised(self, tmp_path):
        executor = _make_executor(tmp_path)
        task = MagicMock()
        task.skill = "health-check"

        mock_module = MagicMock()
        mock_module.run = AsyncMock(return_value="ok")

        with patch.object(executor, "_build_skill_context", return_value=MagicMock()), \
             patch("importlib.import_module", return_value=mock_module) as mock_import:
            asyncio.get_event_loop().run_until_complete(
                executor._invoke_skill(task, None, "run-1")
            )
            mock_import.assert_called_once_with("everbot.core.jobs.health_check")

    def test_disallowed_skill_raises_valueerror(self, tmp_path):
        executor = _make_executor(tmp_path)
        task = MagicMock()
        task.skill = "os.system"

        with pytest.raises(ValueError, match="not in the allowed skills whitelist"):
            asyncio.get_event_loop().run_until_complete(
                executor._invoke_skill(task, None, "run-1")
            )

    def test_path_traversal_skill_rejected(self, tmp_path):
        executor = _make_executor(tmp_path)
        task = MagicMock()
        task.skill = "..evil_module"

        with pytest.raises(ValueError, match="not in the allowed skills whitelist"):
            asyncio.get_event_loop().run_until_complete(
                executor._invoke_skill(task, None, "run-1")
            )


class TestHeartbeatSkillWhitelist:
    """HeartbeatRunner._invoke_skill_task rejects skills not in the whitelist."""

    def test_disallowed_skill_raises_valueerror(self, tmp_path):
        """Heartbeat imports ALLOWED_SKILLS from cron and applies the same check."""
        from src.everbot.core.runtime.heartbeat import HeartbeatRunner

        runner = MagicMock(spec=HeartbeatRunner)
        runner._build_skill_context = MagicMock(return_value=MagicMock())
        runner._write_heartbeat_event = MagicMock()

        task = MagicMock()
        task.skill = "malicious_module"

        with pytest.raises(ValueError, match="not in the allowed skills whitelist"):
            asyncio.get_event_loop().run_until_complete(
                HeartbeatRunner._invoke_skill_task(runner, task, None, "run-1")
            )
