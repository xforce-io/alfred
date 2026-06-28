"""
CLI 命令接口
"""

import asyncio
import argparse
import logging
import signal
from pathlib import Path

from ..infra.user_data import get_user_data_manager
from ..infra.config import load_config, get_config, save_config, get_default_config
from ..infra.log_cleanup import cleanup_alfred_logs
from ..infra.logging_utils import configure_daemon_logging
from .daemon import EverBotDaemon
from .launch_agent import cmd_service_install, cmd_service_status, cmd_service_uninstall
from ..core.runtime.control import get_local_status, run_heartbeat_once
from .doctor import collect_doctor_report
from .memory_cli import register_memory_cli
from .skills_cli import register_skills_cli

logger = logging.getLogger(__name__)


def cmd_init(args):
    """初始化 Agent 工作区"""
    user_data = get_user_data_manager()
    user_data.ensure_directories()

    if args.agent:
        user_data.init_agent_workspace(args.agent)
        print(f"Agent 工作区已初始化: {args.agent}")
        print(f"路径: {user_data.get_agent_dir(args.agent)}")

        # 自动注册到 config.yaml
        config = load_config()  # mutable copy for save_config
        agents = config.setdefault("everbot", {}).setdefault("agents", {})
        if args.agent not in agents:
            workspace_path = str(user_data.get_agent_dir(args.agent))
            agents[args.agent] = {
                "workspace": workspace_path,
                "heartbeat": {
                    "enabled": True,
                    "interval": 30,
                    "active_hours": [8, 22],
                },
            }
            save_config(config)
            print(f"已注册到配置: {user_data.config_path}")
        else:
            print(f"配置中已存在 agent: {args.agent}")
    else:
        print("EverBot 目录已初始化")
        print(f"路径: {user_data.alfred_home}")


def cmd_list(args):
    """列出所有 Agent"""
    user_data = get_user_data_manager()
    agents = user_data.list_agents()

    if not agents:
        print("暂无 Agent")
        return

    print(f"共 {len(agents)} 个 Agent:")
    for agent in agents:
        print(f"  - {agent}")


def cmd_status(args):
    """查看状态"""
    status = get_local_status(get_user_data_manager())
    running = bool(status.get("running"))
    pid = status.get("pid")
    snapshot = status.get("snapshot") or {}

    print(f"EverBot: {'运行中' if running else '未运行'}" + (f" (pid={pid})" if pid else ""))

    agents = snapshot.get("agents", []) if isinstance(snapshot, dict) else []
    if agents:
        print(f"Agents: {', '.join(agents)}")

    # #93 件B:每 agent 生效模型 vs 配置目标(stale = 改了配置但未重启生效)。
    _print_agent_model_states()

    # #132:陈旧的 per-agent skill 覆盖副本(静默遮蔽仓库修复)。
    _print_stale_skill_overrides()

    hb = (snapshot.get("heartbeats", {}) if isinstance(snapshot, dict) else {}) or {}
    if hb:
        print("最近心跳:")
        for agent_name, state in hb.items():
            ts = (state or {}).get("timestamp", "")
            preview = (state or {}).get("result_preview", "")
            if ts:
                print(f"  - {agent_name}: {ts} {preview[:80]}")


def _print_agent_model_states() -> None:
    """打印每个配置 agent 的生效模型 / 配置目标 / stale 标记(#93 件B)。"""
    try:
        from .agent_model_state import collect_agent_model_states
        from ..core.agent.agent_config import resolve_agent_model
        from ..core.agent.provider.model_config import load_model_config

        config = get_config()
        agents = list(((config.get("everbot") or {}).get("agents") or {}).keys())
        if not agents:
            return
        milkie_root = Path(
            ((config.get("everbot") or {}).get("milkie") or {}).get("data_dir_root")
            or (get_user_data_manager().alfred_home / "milkie")
        ).expanduser()
        mc = load_model_config()

        def _configured(agent_name: str):
            key = resolve_agent_model(agent_name)
            return (mc.llms.get(key) or {}).get("model_name") if key else None

        states = collect_agent_model_states(
            agents, milkie_root=milkie_root, configured_resolver=_configured
        )
    except Exception as e:  # status 是只读自检,绝不因此崩
        logging.getLogger(__name__).debug("agent model state collect failed: %s", e)
        return

    print("模型(生效 / 配置):")
    for s in states:
        eff = s["effective"] or "—(sidecar 未拉起)"
        cfg = s["configured"] or "?"
        flag = "  ⚠️ STALE 待重启生效" if s["stale"] else ""
        print(f"  - {s['agent']}: {eff} / {cfg}{flag}")


def _print_stale_skill_overrides() -> None:
    """打印每个配置 agent 的陈旧 skill 覆盖告警(#132)。

    陈旧 = per-agent 覆盖副本内容偏离仓库内置版,且不比仓库新(即上游修复
    被静默遮蔽)。只读自检,绝不因此崩。
    """
    try:
        udm = get_user_data_manager()
        config = get_config()
        agents = list(((config.get("everbot") or {}).get("agents") or {}).keys())
        rows = [(a, udm.list_stale_skill_overrides(a)) for a in agents]
        rows = [(a, skills) for a, skills in rows if skills]
    except Exception as e:  # status 是只读自检,绝不因此崩
        logging.getLogger(__name__).debug("stale skill override check failed: %s", e)
        return

    if not rows:
        return
    print("陈旧 skill 覆盖(per-agent 副本遮蔽仓库修复,#132):")
    for agent, skills in rows:
        print(f"  - {agent}: {', '.join(skills)}  ⚠️ 建议同步或移除覆盖")


async def cmd_start_async(args):
    """启动守护进程（异步）"""
    daemon = EverBotDaemon(
        config_path=args.config,
        global_config_path=getattr(args, 'dolphin_config', None),
        default_model=getattr(args, 'model', None),
    )
    await daemon.start()


def cmd_start(args):
    """启动守护进程（由 bin/everbot 管理进程）"""
    log_level = args.log_level or "INFO"
    configure_daemon_logging(level=log_level)

    # 启动守护进程
    asyncio.run(cmd_start_async(args))


def cmd_config(args):
    """配置管理"""
    if args.show:
        # 显示当前配置
        config = get_config(args.config)
        import yaml
        print(yaml.dump(config, default_flow_style=False, allow_unicode=True))
    elif args.init:
        # 初始化默认配置
        config = get_default_config()
        save_config(config, args.config)
        print(f"配置已初始化: {args.config or '~/.alfred/config.yaml'}")
    elif getattr(args, "impact", False):
        _print_config_impact(args.config)
    else:
        print("使用 --show 查看配置，--init 初始化配置，--impact 查看模型变更爆炸半径")


def _print_config_impact(config_path=None) -> None:
    """打印 agent→llm key→(cloud, model_name) 映射 + 共享标记(#94 件A)。"""
    from .config_impact import build_config_impact
    from ..core.agent.agent_config import resolve_agent_model
    from ..core.agent.provider.model_config import load_model_config

    config = get_config(config_path)
    agents = list(((config.get("everbot") or {}).get("agents") or {}).keys())
    if not agents:
        print("配置中无 agent。")
        return
    mc = load_model_config()
    agent_keys = {a: (resolve_agent_model(a) or "?") for a in agents}
    rows = build_config_impact(agent_keys, mc.llms)

    print("配置变更爆炸半径(agent → llm key → cloud/model):")
    for r in rows:
        shared = f"  ⚠️ 共享(改 model_name 连带影响: {', '.join(r['shared_with'])})" if r["shared_with"] else ""
        print(f"  - {r['agent']}: {r['key']} → {r['cloud']}/{r['model_name']}{shared}")


def cmd_stop(args):
    """停止守护进程 + Web 服务器"""
    user_data = get_user_data_manager()
    stopped_any = False

    # 停止 daemon
    daemon_pid_file = user_data.pid_file
    if daemon_pid_file.exists():
        try:
            daemon_pid = int(daemon_pid_file.read_text().strip())
            import os
            os.kill(daemon_pid, signal.SIGTERM)
            print(f"EverBot 守护进程已停止 (pid={daemon_pid})")
            daemon_pid_file.unlink()
            stopped_any = True
        except (OSError, ProcessLookupError, ValueError) as e:
            print(f"停止守护进程失败: {e}")
            daemon_pid_file.unlink(missing_ok=True)

    # 停止 web 服务器
    web_pid_file = user_data.alfred_home / "everbot-web.pid"
    if web_pid_file.exists():
        try:
            web_pid = int(web_pid_file.read_text().strip())
            import os
            os.kill(web_pid, signal.SIGTERM)
            print(f"Web 服务器已停止 (pid={web_pid})")
            web_pid_file.unlink()
            stopped_any = True
        except (OSError, ProcessLookupError, ValueError) as e:
            print(f"停止 Web 服务器失败: {e}")
            web_pid_file.unlink(missing_ok=True)

    if not stopped_any:
        print("EverBot 未在运行")


def cmd_heartbeat(args):
    """手动触发心跳"""
    print(f"手动触发心跳: {args.agent}")
    result = asyncio.run(
        run_heartbeat_once(
            args.agent,
            config_path=args.config,
            dolphin_config_path=getattr(args, "dolphin_config", None),
            model=getattr(args, "model", None),
            force=bool(getattr(args, "force", False)),
        )
    )
    print(f"结果: {result[:400]}")


def cmd_migrate_agent(args):
    """已废弃:迁移 agent.dph 是 dolphin 专属(#38 已移除 dolphin)。

    milkie runtime 不使用 .dph;agent 配置由 milkie agent.md(运行时生成)+ 工作区
    SOUL/AGENTS/SKILLS/USER/MEMORY.md 描述,无需迁移。
    """
    print(
        "migrate-agent 已废弃:dolphin 已移除(#38),milkie 不使用 agent.dph。\n"
        "无需任何迁移动作。"
    )


def cmd_doctor(args):
    """运行环境自检"""
    project_root = Path(__file__).resolve().parents[3]
    items = collect_doctor_report(project_root=project_root)

    level_order = {"ERROR": 0, "WARN": 1, "OK": 2}
    items = sorted(items, key=lambda x: (level_order.get(x.level, 9), x.title))

    ok = sum(1 for x in items if x.level == "OK")
    warn = sum(1 for x in items if x.level == "WARN")
    err = sum(1 for x in items if x.level == "ERROR")

    print(f"Doctor: OK={ok} WARN={warn} ERROR={err}")
    for item in items:
        prefix = {"OK": "✓", "WARN": "!", "ERROR": "✗"}.get(item.level, "-")
        print(f"{prefix} [{item.level}] {item.title}: {item.details}")
        if item.hint:
            print(f"    Hint: {item.hint}")


def cmd_cleanup_logs(args):
    """Clean up historical Alfred logs and migrate legacy skill logs."""
    user_data = get_user_data_manager(
        Path(args.alfred_home).expanduser() if args.alfred_home else None
    )
    summary = cleanup_alfred_logs(
        user_data=user_data,
        dry_run=not args.apply,
        agent_name=getattr(args, "agent", "") or "",
    )

    mode = "执行" if args.apply else "预演"
    print(f"日志清理{mode}完成")
    print(f"扫描文件: {summary.files_scanned}")
    print(f"更新文件: {summary.files_updated}")
    print(f"创建备份: {summary.backups_created}")
    print(f"脱敏行数: {summary.lines_redacted}")
    print(f"迁移 segment: {summary.skill_segments_migrated}")
    if not args.apply:
        print("当前为预演模式；加上 --apply 才会真正写入。")


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="EverBot - Ever Running Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # 全局参数
    parser.add_argument("--config", type=str, help="配置文件路径")
    parser.add_argument("--log-level", type=str,
                       choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                       help="日志级别")

    subparsers = parser.add_subparsers(dest="command", help="子命令")

    # init 命令
    parser_init = subparsers.add_parser("init", help="初始化工作区")
    parser_init.add_argument("agent", nargs="?", help="Agent 名称")
    parser_init.set_defaults(func=cmd_init)

    # list 命令
    parser_list = subparsers.add_parser("list", help="列出所有 Agent")
    parser_list.set_defaults(func=cmd_list)

    # start 命令（推荐使用 bin/everbot start）
    parser_start = subparsers.add_parser("start", help="启动守护进程（推荐使用 bin/everbot）")
    parser_start.add_argument("--dolphin-config", type=str, help="Dolphin 配置文件路径")
    parser_start.add_argument("--model", type=str, default=None, help="默认模型（为空则使用 Dolphin 配置默认）")
    parser_start.set_defaults(func=cmd_start)

    # stop 命令
    parser_stop = subparsers.add_parser("stop", help="停止守护进程 + Web 服务器")
    parser_stop.set_defaults(func=cmd_stop)

    # status 命令
    parser_status = subparsers.add_parser("status", help="查看状态")
    parser_status.set_defaults(func=cmd_status)

    # config 命令
    parser_config = subparsers.add_parser("config", help="配置管理")
    parser_config.add_argument("--show", action="store_true", help="显示当前配置")
    parser_config.add_argument("--init", action="store_true", help="初始化默认配置")
    parser_config.add_argument("--impact", action="store_true", help="查看模型变更爆炸半径(agent→模型,标共享 key)")
    parser_config.set_defaults(func=cmd_config)

    # heartbeat 命令
    parser_heartbeat = subparsers.add_parser("heartbeat", help="手动触发心跳")
    parser_heartbeat.add_argument("--agent", required=True, help="Agent 名称")
    parser_heartbeat.add_argument("--force", action="store_true", help="忽略活跃时段限制")
    parser_heartbeat.add_argument("--dolphin-config", type=str, help="Dolphin 配置文件路径")
    parser_heartbeat.add_argument("--model", type=str, default=None, help="默认模型（为空则使用 Dolphin 配置默认）")
    parser_heartbeat.set_defaults(func=cmd_heartbeat)

    # migrate-agent 命令
    parser_migrate = subparsers.add_parser("migrate-agent", help="迁移/修复 agent.dph（兼容旧格式）")
    parser_migrate.add_argument("--agent", required=True, help="Agent 名称")
    parser_migrate.add_argument("--dolphin-config", type=str, help="Dolphin 配置文件路径")
    parser_migrate.set_defaults(func=cmd_migrate_agent)

    # doctor 命令
    parser_doctor = subparsers.add_parser("doctor", help="环境自检（配置/技能/依赖/工作区）")
    parser_doctor.set_defaults(func=cmd_doctor)

    parser_cleanup = subparsers.add_parser("cleanup-logs", help="清理历史日志并迁移旧 skill_logs")
    parser_cleanup.add_argument("--alfred-home", type=str, help="Alfred home 路径，默认使用 ~/.alfred 或 $ALFRED_HOME")
    parser_cleanup.add_argument("--agent", type=str, help="只处理指定 agent 的 skill_logs")
    parser_cleanup.add_argument("--apply", action="store_true", help="实际写入变更；默认仅预演")
    parser_cleanup.set_defaults(func=cmd_cleanup_logs)

    parser_service_install = subparsers.add_parser("service-install", help="安装 macOS LaunchAgent 常驻服务")
    parser_service_install.set_defaults(func=cmd_service_install)

    parser_service_uninstall = subparsers.add_parser("service-uninstall", help="卸载 macOS LaunchAgent 常驻服务")
    parser_service_uninstall.set_defaults(func=cmd_service_uninstall)

    parser_service_status = subparsers.add_parser("service-status", help="查看 macOS LaunchAgent 状态")
    parser_service_status.set_defaults(func=cmd_service_status)

    # memory 命令（记忆系统观测）
    register_memory_cli(subparsers)

    # skills 命令（技能管理）
    register_skills_cli(subparsers)

    return parser


def main():
    """CLI 主入口"""
    parser = _build_parser()

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # 执行命令
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
