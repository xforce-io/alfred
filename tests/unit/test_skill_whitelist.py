"""Unit tests for skill import whitelist validation in CronExecutor and HeartbeatRunner."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.everbot.core.runtime.cron import ALLOWED_JOBS, CronExecutor
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


class TestAllowedJobsWhitelist:
    """Verify the ALLOWED_JOBS constant is well-formed."""

    def test_contains_expected_jobs(self):
        assert "health_check" in ALLOWED_JOBS
        assert "memory_review" in ALLOWED_JOBS
        assert "task_discover" in ALLOWED_JOBS

    def test_is_frozenset(self):
        assert isinstance(ALLOWED_JOBS, frozenset)


class TestCronJobWhitelist:
    """CronExecutor._invoke_job rejects jobs not in the whitelist."""

    @pytest.mark.asyncio
    async def test_allowed_job_is_imported(self, tmp_path):
        executor = _make_executor(tmp_path)
        task = MagicMock()
        task.job = "health_check"

        mock_module = MagicMock()
        mock_module.run = AsyncMock(return_value="ok")

        with patch.object(executor, "_build_job_context", return_value=MagicMock()), \
             patch("importlib.import_module", return_value=mock_module) as mock_import:
            result = await executor._invoke_job(task, None, "run-1")
            mock_import.assert_called_once_with("src.everbot.core.jobs.health_check")
            assert result == "ok"

    @pytest.mark.asyncio
    async def test_hyphenated_job_normalised(self, tmp_path):
        executor = _make_executor(tmp_path)
        task = MagicMock()
        task.job = "health-check"

        mock_module = MagicMock()
        mock_module.run = AsyncMock(return_value="ok")

        with patch.object(executor, "_build_job_context", return_value=MagicMock()), \
             patch("importlib.import_module", return_value=mock_module) as mock_import:
            await executor._invoke_job(task, None, "run-1")
            mock_import.assert_called_once_with("src.everbot.core.jobs.health_check")

    @pytest.mark.asyncio
    async def test_disallowed_job_raises_valueerror(self, tmp_path):
        executor = _make_executor(tmp_path)
        task = MagicMock()
        task.job = "os.system"

        with pytest.raises(ValueError, match="not in the allowed jobs whitelist"):
            await executor._invoke_job(task, None, "run-1")

    @pytest.mark.asyncio
    async def test_path_traversal_job_rejected(self, tmp_path):
        executor = _make_executor(tmp_path)
        task = MagicMock()
        task.job = "..evil_module"

        with pytest.raises(ValueError, match="not in the allowed jobs whitelist"):
            await executor._invoke_job(task, None, "run-1")


class TestJobImportPathResolvable:
    """The import path used in _invoke_job must actually resolve.

    Production bug: cron.py used 'everbot.core.jobs.{name}' but the package
    is 'src.everbot', so importlib.import_module failed with
    'No module named everbot'.  Existing tests mocked importlib and never
    caught this.
    """

    @pytest.mark.parametrize("job_name", sorted(ALLOWED_JOBS))
    def test_job_module_is_importable(self, job_name):
        """Each whitelisted job must be importable without mocking."""
        import importlib
        module_path = f"src.everbot.core.jobs.{job_name}"
        try:
            mod = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            pytest.fail(
                f"Job '{job_name}' not importable at '{module_path}': {exc}"
            )
        assert hasattr(mod, "run"), f"{module_path} missing 'run' entry point"

    def test_cron_invoke_uses_resolvable_path(self, tmp_path):
        """CronExecutor._invoke_job must use an import path that works
        without mocking importlib."""
        import importlib
        from src.everbot.core.runtime.cron import CronExecutor
        import inspect

        source = inspect.getsource(CronExecutor._invoke_job)
        # The import_module call must NOT use bare 'everbot.core.jobs'
        assert "\"everbot.core.jobs." not in source and "'everbot.core.jobs." not in source, (
            "CronExecutor._invoke_job uses 'everbot.core.jobs.*' which is "
            "not importable — the package is 'src.everbot'. "
            "Use a relative or correct absolute import path."
        )


class TestHeartbeatJobWhitelist:
    """HeartbeatRunner._invoke_skill_task rejects jobs not in the whitelist."""

    @pytest.mark.asyncio
    async def test_disallowed_job_raises_valueerror(self, tmp_path):
        """Heartbeat imports ALLOWED_JOBS from cron and applies the same check."""
        from src.everbot.core.runtime.heartbeat import HeartbeatRunner

        runner = MagicMock(spec=HeartbeatRunner)
        runner._build_skill_context = MagicMock(return_value=MagicMock())
        runner._write_heartbeat_event = MagicMock()

        task = MagicMock()
        task.job = "malicious_module"

        with pytest.raises(ValueError, match="not in the allowed jobs whitelist"):
            await HeartbeatRunner._invoke_skill_task(runner, task, None, "run-1")
