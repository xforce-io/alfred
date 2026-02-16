"""
Skills 管理 CLI 命令
"""

import subprocess
import sys
from pathlib import Path


def get_skill_installer_scripts_dir() -> Path:
    """获取 skill-installer 脚本目录"""
    # 从 everbot 模块推断 alfred 根目录
    alfred_root = Path(__file__).resolve().parents[3]
    scripts_dir = alfred_root / "skills" / "skill-installer" / "scripts"

    if not scripts_dir.exists():
        raise FileNotFoundError(
            f"skill-installer 未找到: {scripts_dir}\n"
            f"请确保 skills/skill-installer 已正确安装"
        )

    return scripts_dir


def run_skill_script(script_name: str, args: list) -> int:
    """运行 skill-installer 脚本"""
    scripts_dir = get_skill_installer_scripts_dir()
    script_path = scripts_dir / f"{script_name}.py"

    if not script_path.exists():
        print(f"错误: 脚本不存在: {script_path}", file=sys.stderr)
        return 1

    # 运行脚本并返回退出码
    cmd = [sys.executable, str(script_path)] + args
    result = subprocess.run(cmd)
    return result.returncode


def cmd_skills_search(args):
    """搜索技能"""
    return run_skill_script("search", [args.query] + (["--json"] if args.json else []))


def cmd_skills_install(args):
    """安装技能"""
    cmd_args = [args.source]
    if args.method:
        cmd_args.extend(["--method", args.method])
    if args.skills_dir:
        cmd_args.extend(["--skills-dir", args.skills_dir])
    return run_skill_script("install", cmd_args)


def cmd_skills_list(args):
    """列出已安装技能"""
    return run_skill_script("list", ["--json"] if args.json else [])


def cmd_skills_update(args):
    """更新技能"""
    cmd_args = []
    if args.all:
        cmd_args.append("--all")
    elif args.skill_name:
        cmd_args.append(args.skill_name)
    else:
        # 默认为 --all
        cmd_args.append("--all")
    return run_skill_script("update", cmd_args)


def cmd_skills_remove(args):
    """删除技能"""
    cmd_args = []
    if args.interactive:
        cmd_args.append("--interactive")
    elif args.list:
        cmd_args.append("--list")
    elif args.skill_names:
        cmd_args.extend(args.skill_names)
    else:
        print("错误: 请指定要删除的技能名称，或使用 --interactive 或 --list", file=sys.stderr)
        return 1

    if args.force:
        cmd_args.append("--force")
    if args.backup:
        cmd_args.append("--backup")

    return run_skill_script("remove", cmd_args)


def cmd_skills_enable(args):
    """启用技能"""
    return run_skill_script("enable", [args.skill_name])


def cmd_skills_disable(args):
    """禁用技能"""
    return run_skill_script("disable", [args.skill_name])


def cmd_skills_default(args):
    """默认 skills 命令（显示列表）"""
    return run_skill_script("list", [])


def register_skills_cli(subparsers):
    """
    注册 skills 子命令

    使用方式:
        everbot skills list
        everbot skills search <query>
        everbot skills install <source>
        everbot skills update [skill_name]
        everbot skills remove <skill_name>
    """
    # 创建 skills 命令组
    parser_skills = subparsers.add_parser(
        "skills",
        help="管理 Alfred 技能",
        description="管理 Alfred 技能 - 搜索、安装、更新、删除技能"
    )

    # skills 子命令
    skills_subparsers = parser_skills.add_subparsers(
        dest="skills_command",
        help="skills 子命令"
    )

    # skills search
    parser_search = skills_subparsers.add_parser(
        "search",
        help="搜索技能注册表"
    )
    parser_search.add_argument("query", help="搜索关键词")
    parser_search.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser_search.set_defaults(func=cmd_skills_search)

    # skills install
    parser_install = skills_subparsers.add_parser(
        "install",
        help="安装技能"
    )
    parser_install.add_argument("source", help="技能名称、Git URL 或本地路径")
    parser_install.add_argument(
        "--method",
        choices=["registry", "git", "url", "local"],
        help="安装方式（自动检测如不指定）"
    )
    parser_install.add_argument("--skills-dir", help="自定义技能目录")
    parser_install.set_defaults(func=cmd_skills_install)

    # skills list
    parser_list = skills_subparsers.add_parser(
        "list",
        help="列出已安装技能"
    )
    parser_list.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser_list.set_defaults(func=cmd_skills_list)

    # skills update
    parser_update = skills_subparsers.add_parser(
        "update",
        help="更新技能"
    )
    parser_update.add_argument("skill_name", nargs="?", help="技能名称（不指定则更新全部）")
    parser_update.add_argument("--all", action="store_true", help="更新所有技能")
    parser_update.set_defaults(func=cmd_skills_update)

    # skills remove
    parser_remove = skills_subparsers.add_parser(
        "remove",
        help="删除技能"
    )
    parser_remove.add_argument("skill_names", nargs="*", help="技能名称（可多个）")
    parser_remove.add_argument("-f", "--force", action="store_true", help="跳过确认")
    parser_remove.add_argument("--backup", action="store_true", help="保留备份")
    parser_remove.add_argument("-i", "--interactive", action="store_true", help="交互式选择")
    parser_remove.add_argument("--list", action="store_true", help="列出可删除的技能")
    parser_remove.set_defaults(func=cmd_skills_remove)

    # skills enable
    parser_enable = skills_subparsers.add_parser(
        "enable",
        help="启用技能"
    )
    parser_enable.add_argument("skill_name", help="技能名称")
    parser_enable.set_defaults(func=cmd_skills_enable)

    # skills disable
    parser_disable = skills_subparsers.add_parser(
        "disable",
        help="禁用技能"
    )
    parser_disable.add_argument("skill_name", help="技能名称")
    parser_disable.set_defaults(func=cmd_skills_disable)

    # 默认动作（无子命令时显示列表）
    parser_skills.set_defaults(func=cmd_skills_default)
