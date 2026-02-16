"""
EverBot 守护进程
"""

import asyncio
import inspect
import os
import signal
import logging
from pathlib import Path
from typing import Dict, Optional, Any
from datetime import datetime
import json
import uuid

from ..core.runtime.heartbeat import HeartbeatRunner
from ..core.session.session import SessionManager
from ..infra.user_data import UserDataManager
from ..infra.config import load_config
from ..core.agent.factory import get_agent_factory
from ..infra.process import DaemonLock, write_pid_file, remove_pid_file
from ..core.runtime.scheduler import AgentSchedule, Scheduler, SchedulerTask
from ..channels.telegram_channel import TelegramChannel

logger = logging.getLogger(__name__)


class EverBotDaemon:
    """
    EverBot 守护进程

    管理多个 Agent 的心跳和生命周期。
    """

    def __init__(
        self,
        config_path: Optional[str] = None,
        global_config_path: Optional[str] = None,
        default_model: Optional[str] = None,
    ):
        self.config = load_config(config_path)
        self.user_data = UserDataManager()
        self.session_manager: Optional[SessionManager] = None
        self.heartbeat_runners: Dict[str, HeartbeatRunner] = {}
        self._heartbeat_state: Dict[str, Dict[str, str]] = {}
        self._started_at = datetime.now().isoformat()
        self._pid: Optional[int] = None
        runtime_cfg = (self.config.get("everbot", {}) or {}).get("runtime", {}) or {}
        self._job_retention_days = int(runtime_cfg.get("job_retention_days", 7) or 7)
        self._job_max_archived_sessions = int(runtime_cfg.get("job_max_archived_sessions", 200) or 200)
        self._job_cleanup_interval_seconds = max(
            30,
            int(runtime_cfg.get("job_cleanup_interval_seconds", 600) or 600),
        )
        self._scheduler_cron_jobs = bool(runtime_cfg.get("scheduler_cron_jobs", True))
        self._scheduler: Optional[Scheduler] = None
        self._legacy_runner_tasks: Dict[str, asyncio.Task] = {}
        self._telegram_channel: Optional[TelegramChannel] = None
        self._daemon_lock: Optional[DaemonLock] = None

        self.agent_factory = get_agent_factory(
            global_config_path=global_config_path,
            default_model=default_model,
        )

        self._running = False

    # -- Scheduler callbacks ------------------------------------------------

    async def _scheduler_run_heartbeat(self, agent_name: str, ts: datetime) -> None:
        """Scheduler callback: execute one heartbeat tick for an agent.

        Exceptions propagate to the Scheduler so it can track consecutive
        failures and apply exponential backoff.
        """
        runner = self.heartbeat_runners.get(agent_name)
        if runner is None:
            return
        # When the unified scheduler drives heartbeats, the runner should
        # skip inline/isolated tasks if cron_jobs are separately managed.
        await self._run_runner_with_options(
            runner,
            include_inline=(not self._scheduler_cron_jobs),
            include_isolated=(not self._scheduler_cron_jobs),
        )

    async def _run_runner_with_options(
        self,
        runner: Any,
        *,
        include_inline: bool,
        include_isolated: bool,
    ) -> None:
        """Run one heartbeat tick with compatibility options."""
        run_once = getattr(runner, "run_once_with_options", None)
        if not callable(run_once):
            start_runner = getattr(runner, "start", None)
            if callable(start_runner):
                runner_key = str(getattr(runner, "agent_name", id(runner)))
                task = self._legacy_runner_tasks.get(runner_key)
                if task is None or task.done():
                    self._legacy_runner_tasks[runner_key] = asyncio.create_task(start_runner())
            return
        kwargs: Dict[str, Any] = {"force": False}
        try:
            sig = inspect.signature(run_once)
            if "include_inline" in sig.parameters:
                kwargs["include_inline"] = include_inline
            if "include_isolated" in sig.parameters:
                kwargs["include_isolated"] = include_isolated
        except (TypeError, ValueError):
            pass
        result = await run_once(**kwargs)
        if result == "HEARTBEAT_FAILED":
            raise RuntimeError("Heartbeat returned HEARTBEAT_FAILED")

    # -- Cron task callbacks ------------------------------------------------

    def _build_scheduler(self) -> Scheduler:
        """Build the unified scheduler with all callbacks wired."""
        isolated_lookup: Dict[str, tuple[Any, Dict[str, Any]]] = {}

        def _collect_due_tasks(ts: datetime) -> list[SchedulerTask]:
            isolated_lookup.clear()
            due: list[SchedulerTask] = []
            for agent_name, runner in self.heartbeat_runners.items():
                for mode_fn, exec_mode in [
                    ("list_due_inline_tasks", "inline"),
                    ("list_due_isolated_tasks", "isolated"),
                ]:
                    list_fn = getattr(runner, mode_fn, None)
                    if not callable(list_fn):
                        continue
                    for snapshot in (list_fn(now=ts) or []):
                        task_id = str(snapshot.get("id") or "").strip()
                        if not task_id:
                            continue
                        key = f"{agent_name}:{task_id}"
                        isolated_lookup[key] = (runner, snapshot)
                        due.append(SchedulerTask(
                            id=key,
                            agent_name=agent_name,
                            execution_mode=exec_mode,
                            timeout_seconds=int(snapshot.get("timeout_seconds", 120) or 120),
                        ))
            return due

        async def _claim_task(task_key: str) -> bool:
            target = isolated_lookup.get(task_key)
            if target is None:
                return False
            runner, snapshot = target
            claim = getattr(runner, "claim_isolated_task", None)
            if not callable(claim):
                return False
            task_id = str(snapshot.get("id") or "").strip()
            return bool(await claim(task_id)) if task_id else False

        async def _run_inline(agent_name: str, _tasks: list[SchedulerTask], _ts: datetime) -> None:
            runner = self.heartbeat_runners.get(agent_name)
            if runner is None:
                return
            await self._run_runner_with_options(runner, include_inline=True, include_isolated=False)

        async def _run_isolated(task: SchedulerTask, ts: datetime) -> None:
            target = isolated_lookup.get(task.id)
            if target is None:
                return
            runner, snapshot = target
            execute = getattr(runner, "execute_isolated_claimed_task", None)
            if not callable(execute):
                return
            run_id = f"job_{uuid.uuid4().hex[:12]}"
            await execute(snapshot, run_id=run_id, now=ts)

        # Build agent schedules from registered runners
        agent_schedules: Dict[str, AgentSchedule] = {}
        for agent_name, runner in self.heartbeat_runners.items():
            agent_schedules[agent_name] = AgentSchedule(
                agent_name=agent_name,
                interval_minutes=max(1, int(getattr(runner, "interval_minutes", 30) or 30)),
                active_hours=tuple(getattr(runner, "active_hours", (8, 22))),
            )

        return Scheduler(
            run_heartbeat=self._scheduler_run_heartbeat,
            get_due_tasks=_collect_due_tasks if self._scheduler_cron_jobs else None,
            claim_task=_claim_task if self._scheduler_cron_jobs else None,
            run_inline=_run_inline if self._scheduler_cron_jobs else None,
            run_isolated=_run_isolated if self._scheduler_cron_jobs else None,
            agent_schedules=agent_schedules,
            tick_interval_seconds=1.0,
            state_file=self.user_data.alfred_home / "scheduler_state.json",
        )

    # -- Job cleanup --------------------------------------------------------

    async def _run_job_cleanup_loop(self) -> None:
        """Periodically cleanup archived job sessions."""
        while self._running:
            try:
                if self.session_manager is not None and hasattr(self.session_manager, "cleanup_archived_job_sessions"):
                    removed = await self.session_manager.cleanup_archived_job_sessions(
                        retention_days=self._job_retention_days,
                        max_sessions=self._job_max_archived_sessions,
                    )
                    if removed > 0:
                        logger.info("Archived job session cleanup removed %s files", removed)
            except Exception as exc:
                logger.warning("Archived job session cleanup failed: %s", exc)
            await asyncio.sleep(self._job_cleanup_interval_seconds)

    # -- Init / status ------------------------------------------------------

    async def _init_components(self):
        """初始化组件"""
        self.user_data.ensure_directories()
        self.session_manager = SessionManager(self.user_data.sessions_dir)
        logger.info("组件初始化完成")

    def _write_status_snapshot(self) -> None:
        """Write a status snapshot to disk for CLI/Web consumption."""
        task_states: Dict[str, Any] = {}
        for name, runner in self.heartbeat_runners.items():
            tl = getattr(runner, "_task_list", None)
            if tl is not None:
                task_states[name] = tl.to_dict()

        snapshot = {
            "status": "running" if self._running else "stopped",
            "pid": self._pid,
            "started_at": self._started_at,
            "timestamp": datetime.now().isoformat(),
            "agents": list(self.heartbeat_runners.keys()),
            "heartbeats": self._heartbeat_state,
            "task_states": task_states,
            "metrics": self.session_manager.get_metrics_snapshot() if self.session_manager else {},
        }
        try:
            self.user_data.status_file.parent.mkdir(parents=True, exist_ok=True)
            self.user_data.status_file.write_text(
                json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"Failed to write status snapshot: {e}")

    async def _on_heartbeat_result(self, agent_name: str, result: str):
        """心跳结果回调"""
        log_file = self.user_data.heartbeat_log_file
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                timestamp = datetime.now().isoformat()
                f.write(f"[{timestamp}] [{agent_name}] {result[:200]}\n")
        except Exception as e:
            logger.error(f"写入心跳日志失败: {e}")
            return

        self._heartbeat_state[agent_name] = {
            "timestamp": datetime.now().isoformat(),
            "result_preview": result[:200],
        }
        self._write_status_snapshot()

    def _create_heartbeat_runners(self):
        """为配置的 Agent 创建心跳运行器"""
        agents_config = self.config.get("everbot", {}).get("agents", {})

        for agent_name, agent_config in agents_config.items():
            heartbeat_config = agent_config.get("heartbeat", {})
            if not heartbeat_config.get("enabled", False):
                logger.info(f"Agent {agent_name} 心跳未启用")
                continue

            workspace_path = Path(agent_config.get(
                "workspace",
                f"~/.alfred/agents/{agent_name}"
            )).expanduser()

            if not workspace_path.exists():
                logger.warning(f"工作区不存在，正在创建: {workspace_path}")
                self.user_data.init_agent_workspace(agent_name)

            runner_kwargs = {
                "agent_name": agent_name,
                "workspace_path": workspace_path,
                "session_manager": self.session_manager,
                "agent_factory": self.agent_factory.create_agent,
                "interval_minutes": heartbeat_config.get("interval", 30),
                "active_hours": tuple(heartbeat_config.get("active_hours", [8, 22])),
                "max_retries": heartbeat_config.get("max_retries", 3),
                "ack_max_chars": heartbeat_config.get("ack_max_chars", 300),
                "realtime_status_hint": heartbeat_config.get("realtime_status_hint", True),
                "broadcast_scope": heartbeat_config.get("broadcast_scope", "agent"),
                "routine_reflection": heartbeat_config.get("routine_reflection", True),
                "auto_register_routines": heartbeat_config.get("auto_register_routines", False),
                "on_result": self._on_heartbeat_result,
                "heartbeat_max_history": int(heartbeat_config.get("heartbeat_max_history", 10)),
                "reflect_force_interval_hours": int(heartbeat_config.get("reflect_force_interval_hours", 24)),
            }
            runner = HeartbeatRunner(**runner_kwargs)
            self.heartbeat_runners[agent_name] = runner
            logger.info(f"注册心跳: {agent_name} (间隔: {heartbeat_config.get('interval', 30)}分钟)")

    # -- Telegram Channel ---------------------------------------------------

    def _create_telegram_channel(self) -> Optional[TelegramChannel]:
        """Create TelegramChannel from config if enabled."""
        channels_cfg = (
            (self.config.get("everbot", {}) or {}).get("channels", {}) or {}
        )
        tg_cfg = channels_cfg.get("telegram", {}) or {}
        if not tg_cfg.get("enabled", False):
            return None

        bot_token = str(tg_cfg.get("bot_token", "") or "")
        # Support ${ENV_VAR} references
        if bot_token.startswith("${") and bot_token.endswith("}"):
            env_key = bot_token[2:-1]
            bot_token = os.environ.get(env_key, "")
        if not bot_token:
            logger.warning("Telegram channel enabled but bot_token is empty")
            return None

        default_agent = str(tg_cfg.get("default_agent", "") or "")
        allowed_ids = tg_cfg.get("allowed_chat_ids")
        if allowed_ids and not isinstance(allowed_ids, list):
            allowed_ids = None

        return TelegramChannel(
            bot_token=bot_token,
            session_manager=self.session_manager,
            default_agent=default_agent,
            allowed_chat_ids=allowed_ids,
        )

    # -- Lifecycle ----------------------------------------------------------

    async def start(self):
        """启动守护进程"""
        # Singleton guard: acquire exclusive file lock before anything else.
        self._daemon_lock = DaemonLock(self.user_data.alfred_home / "everbot.lock")
        self._daemon_lock.acquire()

        self._running = True
        logger.info("EverBot 守护进程启动")
        cleanup_task: Optional[asyncio.Task] = None

        try:
            await self._init_components()
            self._pid = write_pid_file(self.user_data.pid_file)
            self._create_heartbeat_runners()
            self._telegram_channel = self._create_telegram_channel()
            if self._telegram_channel is not None:
                await self._telegram_channel.start()
            self._write_status_snapshot()
            cleanup_task = asyncio.create_task(self._run_job_cleanup_loop())

            if not self.heartbeat_runners:
                logger.warning("无心跳任务，守护进程将保持空闲状态")
                while self._running:
                    await asyncio.sleep(60)
                return

            # All scheduling goes through the unified Scheduler
            self._scheduler = self._build_scheduler()
            logger.info("Daemon running in unified scheduler mode")
            await self._scheduler.run_forever()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"守护进程异常: {e}")
            raise
        finally:
            self._running = False
            if self._telegram_channel is not None:
                try:
                    await self._telegram_channel.stop()
                except Exception as exc:
                    logger.warning("TelegramChannel stop error: %s", exc)
            if cleanup_task is not None:
                cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
            if self._legacy_runner_tasks:
                legacy_tasks = list(self._legacy_runner_tasks.values())
                for task in legacy_tasks:
                    task.cancel()
                await asyncio.gather(*legacy_tasks, return_exceptions=True)
                self._legacy_runner_tasks.clear()
            self._pid = None
            self._write_status_snapshot()
            remove_pid_file(self.user_data.pid_file)
            if self._daemon_lock is not None:
                self._daemon_lock.release()

    async def stop(self):
        """停止守护进程"""
        self._running = False
        if self._telegram_channel is not None:
            try:
                await self._telegram_channel.stop()
            except Exception as exc:
                logger.warning("TelegramChannel stop error: %s", exc)
        if self._scheduler is not None:
            self._scheduler.stop()
        for runner in self.heartbeat_runners.values():
            runner.stop()
        logger.info("EverBot 守护进程已停止")
        self._write_status_snapshot()

    def health_check(self) -> Dict:
        return {
            "status": "running" if self._running else "stopped",
            "timestamp": datetime.now().isoformat(),
            "agents": list(self.heartbeat_runners.keys()),
            "heartbeats": self._heartbeat_state,
            "session_count": len(self.session_manager._agents) if self.session_manager else 0,
        }


async def main():
    """CLI 入口"""
    import argparse

    parser = argparse.ArgumentParser(description="EverBot Daemon")
    parser.add_argument("--config", type=str, help="EverBot 配置文件路径")
    parser.add_argument("--dolphin-config", type=str, help="Dolphin 全局配置文件路径")
    parser.add_argument("--model", type=str, default=None, help="默认模型名称（为空则使用 Dolphin 配置默认）")
    parser.add_argument("--log-level", type=str, default="INFO",
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="日志级别")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    daemon = EverBotDaemon(
        config_path=args.config,
        global_config_path=args.dolphin_config,
        default_model=args.model,
    )

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.stop()))

    await daemon.start()


if __name__ == "__main__":
    asyncio.run(main())
