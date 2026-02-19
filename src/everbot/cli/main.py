"""
CLI 命令接口
"""

import asyncio
import sys
import argparse
import logging
import subprocess
import signal
from pathlib import Path

from ..infra.user_data import UserDataManager
from ..infra.config import load_config, save_config, get_default_config
from .daemon import EverBotDaemon
from ..core.runtime.control import get_local_status, run_heartbeat_once
from .doctor import collect_doctor_report
from .skills_cli import register_skills_cli

logger = logging.getLogger(__name__)


def cmd_init(args):
    """初始化 Agent 工作区"""
    user_data = UserDataManager()
    user_data.ensure_directories()

    if args.agent:
        user_data.init_agent_workspace(args.agent)
        print(f"Agent 工作区已初始化: {args.agent}")
        print(f"路径: {user_data.get_agent_dir(args.agent)}")

        # 自动注册到 config.yaml
        config = load_config()
        agents = config.setdefault("everbot", {}).setdefault("agents", {})
        if args.agent not in agents:
            agents[args.agent] = {
                "workspace": f"~/.alfred/agents/{args.agent}",
                "heartbeat": {
                    "enabled": True,
                    "interval": 30,
                    "active_hours": [8, 22],
                },
            }
            save_config(config)
            print(f"已注册到配置: ~/.alfred/config.yaml")
        else:
            print(f"配置中已存在 agent: {args.agent}")
    else:
        print("EverBot 目录已初始化")
        print(f"路径: {user_data.alfred_home}")


def cmd_list(args):
    """列出所有 Agent"""
    user_data = UserDataManager()
    agents = user_data.list_agents()

    if not agents:
        print("暂无 Agent")
        return

    print(f"共 {len(agents)} 个 Agent:")
    for agent in agents:
        print(f"  - {agent}")


def cmd_status(args):
    """查看状态"""
    status = get_local_status(UserDataManager())
    running = bool(status.get("running"))
    pid = status.get("pid")
    snapshot = status.get("snapshot") or {}

    print(f"EverBot: {'运行中' if running else '未运行'}" + (f" (pid={pid})" if pid else ""))

    agents = snapshot.get("agents", []) if isinstance(snapshot, dict) else []
    if agents:
        print(f"Agents: {', '.join(agents)}")

    hb = (snapshot.get("heartbeats", {}) if isinstance(snapshot, dict) else {}) or {}
    if hb:
        print("最近心跳:")
        for agent_name, state in hb.items():
            ts = (state or {}).get("timestamp", "")
            preview = (state or {}).get("result_preview", "")
            if ts:
                print(f"  - {agent_name}: {ts} {preview[:80]}")


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
    # 配置日志
    log_level = args.log_level or "INFO"
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # 启动守护进程
    asyncio.run(cmd_start_async(args))


def cmd_config(args):
    """配置管理"""
    if args.show:
        # 显示当前配置
        config = load_config(args.config)
        import yaml
        print(yaml.dump(config, default_flow_style=False, allow_unicode=True))
    elif args.init:
        # 初始化默认配置
        config = get_default_config()
        save_config(config, args.config)
        print(f"配置已初始化: {args.config or '~/.alfred/config.yaml'}")
    else:
        print("使用 --show 查看配置，--init 初始化配置")


def cmd_stop(args):
    """停止守护进程 + Web 服务器"""
    user_data = UserDataManager()
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
    """迁移/修复 agent.dph（将旧格式备份到 baks/，并确保使用标准 DPH）"""
    from ..core.agent.factory import AgentFactory
    from ..infra.workspace import WorkspaceLoader

    user_data = UserDataManager()
    agent_dir = user_data.get_agent_dir(args.agent)
    if not agent_dir.exists():
        print(f"Agent 不存在: {args.agent}")
        return

    factory = AgentFactory(
        global_config_path=getattr(args, "dolphin_config", None),
        default_model=None,
    )

    loader = WorkspaceLoader(agent_dir)
    workspace_instructions = loader.build_system_prompt()
    workspace_instructions = factory._append_runtime_paths(
        workspace_instructions=workspace_instructions,
        workspace_path=agent_dir,
    )

    agent_dph_path = agent_dir / "agent.dph"
    if not agent_dph_path.exists():
        print(f"未找到 agent.dph: {agent_dph_path}")
        return

    factory._ensure_compatible_agent_dph(
        agent_name=args.agent,
        workspace_path=agent_dir,
        agent_dph_path=agent_dph_path,
        model_name="unused",
        workspace_instructions=workspace_instructions,
    )
    print(f"迁移完成: {agent_dph_path}")


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


def main():
    """CLI 主入口"""
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

    # skills 命令（技能管理）
    register_skills_cli(subparsers)

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
