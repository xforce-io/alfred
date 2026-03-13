#!/usr/bin/env python3
"""
CLI tool to visualize saved Agent trajectories.

Usage:
  python scripts/view_trajectory.py <trajectory_file>
  python scripts/view_trajectory.py --list              # 列出所有 trajectory 文件
  python scripts/view_trajectory.py --latest            # 查看最新的 trajectory
  python scripts/view_trajectory.py --index 1           # 查看第 1 个 trajectory
"""
import sys
import json
import argparse
import glob as glob_mod
import re
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime


DEFAULT_TRAJECTORY_GLOB = "~/.alfred/agents/*/tmp/trajectory_*.json"


def get_trajectory_dir() -> Path:
    """Get the default trajectory directory.

    For Alfred, trajectories live under each agent's tmp/ directory.
    Return the common parent (~/.alfred/agents) so --list can scan all agents.
    """
    return Path("~/.alfred/agents").expanduser()


def parse_trajectory_filename(filename: str) -> Optional[Tuple[str, str]]:
    """
    解析 trajectory 文件名，提取时间戳和 session_id

    Alfred 格式: trajectory_{session_id}.json
                 trajectory_{session_id}.{timestamp}.json
    Dolphin 格式: alfred_trajectory_{timestamp}_{session_id_prefix}.json
    """
    # Alfred 格式：带时间戳后缀
    match = re.match(r'trajectory_(.+?)\.(\d{8}_\d{6})\.json', filename)
    if match:
        return match.group(2), match.group(1)[:20]

    # Alfred 格式：无时间戳
    match = re.match(r'trajectory_(.+?)\.json', filename)
    if match:
        return "unknown", match.group(1)[:20]

    # Dolphin 格式
    match = re.match(r'alfred_trajectory_(\d{8}_\d{6})_([a-f0-9]{8})\.json', filename)
    if match:
        return match.group(1), match.group(2)

    # Dolphin 旧格式
    match = re.match(r'alfred_trajectory_([a-f0-9-]+)\.json', filename)
    if match:
        return "unknown", match.group(1)[:8]

    return None


def list_trajectory_files(trajectory_dir: Path, limit: int = 20) -> List[Tuple[Path, dict]]:
    """
    列出所有 trajectory 文件，按修改时间倒序排列

    返回: [(filepath, info), ...]
    """
    files = []

    # Scan all agent tmp dirs under ~/.alfred/agents/
    pattern = str(trajectory_dir / "*/tmp/trajectory_*.json")
    for filepath_str in glob_mod.glob(pattern):
        filepath = Path(filepath_str)
        try:
            stat = filepath.stat()
            parsed = parse_trajectory_filename(filepath.name)
            info = {
                "filename": filepath.name,
                "mtime": datetime.fromtimestamp(stat.st_mtime),
                "size": stat.st_size,
                "timestamp": parsed[0] if parsed else "unknown",
                "session_prefix": parsed[1] if parsed else "unknown",
            }
            files.append((filepath, info))
        except Exception:
            continue

    # 按修改时间倒序排序
    files.sort(key=lambda x: x[1]["mtime"], reverse=True)

    return files[:limit]


def print_trajectory_list(files: List[Tuple[Path, dict]], use_rich: bool = True):
    """打印 trajectory 文件列表"""
    if not files:
        print("⚠️  未找到 trajectory 文件")
        return

    if use_rich:
        try:
            from rich.console import Console
            from rich.table import Table

            console = Console()
            table = Table(show_header=True, border_style="cyan")
            table.add_column("序号", style="cyan", width=6)
            table.add_column("创建时间", style="yellow", width=20)
            table.add_column("Session", style="magenta", width=24)
            table.add_column("修改时间", style="green", width=20)
            table.add_column("大小", style="blue", width=10)
            table.add_column("文件名", style="white")

            for i, (filepath, info) in enumerate(files, 1):
                size_kb = info["size"] / 1024
                mtime_str = info["mtime"].strftime("%Y-%m-%d %H:%M:%S")

                # 解析创建时间
                if info["timestamp"] != "unknown":
                    try:
                        dt = datetime.strptime(info["timestamp"], "%Y%m%d_%H%M%S")
                        ctime_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        ctime_str = info["timestamp"]
                else:
                    ctime_str = "未知"

                table.add_row(
                    str(i),
                    ctime_str,
                    info["session_prefix"],
                    mtime_str,
                    f"{size_kb:.1f} KB",
                    info.get("filename", filepath.name)
                )

            console.print()
            console.print(table)
            console.print("\n💡 使用 --index N 查看第 N 个文件")
            console.print("💡 使用 --latest 查看最新的文件\n")
        except ImportError:
            use_rich = False

    if not use_rich:
        print("\n" + "="*100)
        print(f"📁 找到 {len(files)} 个 Trajectory 文件")
        print("="*100)
        print(f"{'序号':<6} {'创建时间':<20} {'Session':<24} {'修改时间':<20} {'大小':<10} {'文件名'}")
        print("-"*100)

        for i, (filepath, info) in enumerate(files, 1):
            size_kb = info["size"] / 1024
            mtime_str = info["mtime"].strftime("%Y-%m-%d %H:%M:%S")

            if info["timestamp"] != "unknown":
                try:
                    dt = datetime.strptime(info["timestamp"], "%Y%m%d_%H%M%S")
                    ctime_str = dt.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    ctime_str = info["timestamp"]
            else:
                ctime_str = "未知"

            print(f"{i:<6} {ctime_str:<20} {info['session_prefix']:<24} {mtime_str:<20} {size_kb:>8.1f} KB {filepath.name}")

        print("="*100)
        print("\n💡 使用 --index N 查看第 N 个文件")
        print("💡 使用 --latest 查看最新的文件\n")


def format_timestamp(ts_str: str) -> str:
    """格式化时间戳"""
    try:
        dt = datetime.fromisoformat(ts_str)
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts_str


def get_message_model(msg: Dict[str, Any]) -> str:
    """从消息中提取模型名称，优先从 metadata 中读取"""
    metadata = msg.get("metadata") or {}
    model = (
        metadata.get("model")
        or metadata.get("model_name")
        or msg.get("model")
    )
    return str(model) if model else "-"


def compute_duration(prev_ts: Optional[str], curr_ts: Optional[str]) -> Optional[float]:
    """计算两条消息之间的耗时，单位秒"""
    if not prev_ts or not curr_ts:
        return None
    try:
        prev = datetime.fromisoformat(prev_ts)
        curr = datetime.fromisoformat(curr_ts)
        delta = (curr - prev).total_seconds()
        if delta < 0:
            return None
        return delta
    except Exception:
        return None


def truncate_text(text: str, max_length: int = 100) -> str:
    """截断过长的文本"""
    if len(text) <= max_length:
        return text
    return text[:max_length] + "..."


def format_tool_call(tool_call: Dict[str, Any]) -> str:
    """格式化工具调用"""
    func_name = tool_call.get("function", {}).get("name", "unknown")
    args = tool_call.get("function", {}).get("arguments", "{}")

    try:
        args_dict = json.loads(args) if isinstance(args, str) else args
        # 只显示关键参数
        key_params = []
        for key, value in args_dict.items():
            if key in ["query", "top_k", "file_path", "content", "table", "cmd", "skill_name", "mode"]:
                val_str = str(value)
                if len(val_str) > 50:
                    val_str = val_str[:50] + "..."
                key_params.append(f"{key}={val_str}")

        param_str = ", ".join(key_params) if key_params else "..."
        return f"{func_name}({param_str})"
    except Exception:
        return f"{func_name}(...)"


def print_message_simple(
    msg: Dict[str, Any],
    index: int,
    total: int,
    model: Optional[str] = None,
    duration: Optional[float] = None
):
    """简单格式输出消息"""
    role = msg.get("role", "unknown")
    content = msg.get("content", "")
    timestamp = msg.get("timestamp", "")
    tool_calls = msg.get("tool_calls", [])
    tool_call_id = msg.get("tool_call_id")

    # 角色图标
    role_icons = {
        "system": "⚙️",
        "user": "👤",
        "assistant": "🤖",
        "tool": "🔧"
    }
    icon = role_icons.get(role, "❓")

    # 时间戳
    time_str = format_timestamp(timestamp) if timestamp else ""

    print(f"\n{'='*80}")
    print(f"{icon} [{index}/{total}] {role.upper()}")
    info_parts = []
    if time_str:
        info_parts.append(f"⏰ {time_str}")
    if model:
        info_parts.append(f"🧠 模型: {model}")
    if duration is not None:
        info_parts.append(f"⏱️ 耗时: {duration:.3f}秒")
    if info_parts:
        print("   " + "  ".join(info_parts))
    print(f"{'='*80}")

    # 内容
    if content:
        # 截断超长内容
        if len(content) > 1000:
            display_content = content[:1000] + f"\n... (还有 {len(content)-1000} 个字符)"
        else:
            display_content = content
        print(f"\n{display_content}")

    # 工具调用
    if tool_calls:
        print(f"\n📞 工具调用 ({len(tool_calls)}):")
        for tc in tool_calls:
            print(f"   • {format_tool_call(tc)}")

    # 工具响应ID
    if tool_call_id:
        print(f"\n🔗 响应工具调用: {tool_call_id}")


def print_message_rich(
    msg: Dict[str, Any],
    index: int,
    total: int,
    console,
    use_markdown: bool = False,
    model: Optional[str] = None,
    duration: Optional[float] = None
):
    """使用 Rich 库格式化输出消息"""
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text
    from rich.markdown import Markdown

    role = msg.get("role", "unknown")
    content = msg.get("content", "")
    timestamp = msg.get("timestamp", "")
    tool_calls = msg.get("tool_calls", [])
    tool_call_id = msg.get("tool_call_id")

    # 角色样式
    role_styles = {
        "system": ("⚙️  SYSTEM", "cyan"),
        "user": ("👤 USER", "green"),
        "assistant": ("🤖 ASSISTANT", "blue"),
        "tool": ("🔧 TOOL", "yellow")
    }
    role_text, role_color = role_styles.get(role, ("❓ UNKNOWN", "white"))

    # 时间戳
    time_str = format_timestamp(timestamp) if timestamp else ""

    # 标题
    title = f"[{index}/{total}] {role_text}"
    if time_str:
        title += f"  ⏰ {time_str}"
    if model:
        title += f"  🧠 模型: {model}"
    if duration is not None:
        title += f"  ⏱️ 耗时: {duration:.3f}秒"

    # 内容面板
    panel_content = []

    if content:
        # 检测是否为 JSON
        try:
            if content.strip().startswith("{") or content.strip().startswith("["):
                json_obj = json.loads(content)
                json_str = json.dumps(json_obj, indent=2, ensure_ascii=False)
                if len(json_str) > 2000:
                    json_str = json_str[:2000] + "\n... (truncated)"
                syntax = Syntax(json_str, "json", theme="monokai", line_numbers=False)
                panel_content.append(syntax)
            else:
                # 普通文本，截断
                if len(content) > 1500:
                    display_content = content[:1500] + f"\n\n[dim]... (还有 {len(content)-1500} 个字符)[/dim]"
                else:
                    display_content = content

                if use_markdown:
                    panel_content.append(Markdown(display_content))
                else:
                    panel_content.append(Text(display_content, style="white"))
        except Exception:
            # 普通文本
            if len(content) > 1500:
                display_content = content[:1500] + f"\n\n[dim]... (还有 {len(content)-1500} 个字符)[/dim]"
            else:
                display_content = content
            panel_content.append(Text(display_content, style="white"))

    # 工具调用
    if tool_calls:
        tool_text = Text(f"\n📞 工具调用 ({len(tool_calls)}):\n", style="bold yellow")
        for tc in tool_calls:
            tool_text.append(f"   • {format_tool_call(tc)}\n", style="cyan")
        panel_content.append(tool_text)

    # 工具响应ID
    if tool_call_id:
        tool_id_text = Text(f"\n🔗 响应工具调用: {tool_call_id}", style="dim")
        panel_content.append(tool_id_text)

    # 组合内容
    if len(panel_content) == 1:
        final_content = panel_content[0]
    else:
        from rich.console import Group
        final_content = Group(*panel_content)

    # 打印面板
    console.print()
    console.print(Panel(
        final_content,
        title=title,
        border_style=role_color,
        expand=False
    ))


def print_summary(data: Dict[str, Any], use_rich: bool = False):
    """打印摘要统计"""
    trajectory = data.get("trajectory", [])
    tools = data.get("tools", [])

    # 统计信息
    role_counts = {}
    tool_call_count = 0

    for msg in trajectory:
        role = msg.get("role", "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1
        tool_calls = msg.get("tool_calls", [])
        tool_call_count += len(tool_calls)

    # 时间范围
    timestamps = [msg.get("timestamp") for msg in trajectory if msg.get("timestamp")]
    time_range = ""
    if timestamps:
        try:
            start_time = datetime.fromisoformat(timestamps[0])
            end_time = datetime.fromisoformat(timestamps[-1])
            duration = (end_time - start_time).total_seconds()
            time_range = f"{start_time.strftime('%Y-%m-%d %H:%M:%S')} ~ {end_time.strftime('%H:%M:%S')} (耗时: {duration:.1f}秒)"
        except Exception:
            pass

    if use_rich:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel

        console = Console()

        # 摘要表格
        summary_table = Table(show_header=False, border_style="cyan", box=None)
        summary_table.add_column("Key", style="cyan bold", width=20)
        summary_table.add_column("Value", style="white")

        summary_table.add_row("总消息数", str(len(trajectory)))
        summary_table.add_row("可用工具数", str(len(tools)))
        summary_table.add_row("工具调用次数", str(tool_call_count))

        for role, count in sorted(role_counts.items()):
            role_icons = {"system": "⚙️", "user": "👤", "assistant": "🤖", "tool": "🔧"}
            icon = role_icons.get(role, "❓")
            summary_table.add_row(f"{icon} {role}", str(count))

        if time_range:
            summary_table.add_row("时间范围", time_range)

        console.print()
        console.print(Panel(
            summary_table,
            title="📊 Trajectory 摘要",
            border_style="green",
            expand=False
        ))

        # 可用工具列表
        if tools:
            console.print()
            tools_table = Table(show_header=True, border_style="blue", box=None)
            tools_table.add_column("工具名称", style="cyan bold", width=30)
            tools_table.add_column("描述", style="white")

            for tool in tools:
                tool_name = tool.get("function", {}).get("name", "unknown")
                tool_desc = tool.get("function", {}).get("description", "")
                # 截断过长的描述
                if len(tool_desc) > 80:
                    tool_desc = tool_desc[:80] + "..."
                tools_table.add_row(tool_name, tool_desc)

            console.print(Panel(
                tools_table,
                title=f"🛠️  可用工具 ({len(tools)})",
                border_style="blue",
                expand=False
            ))
        console.print()
    else:
        print("\n" + "="*80)
        print("📊 Trajectory 摘要")
        print("="*80)
        print(f"总消息数:      {len(trajectory)}")
        print(f"可用工具数:    {len(tools)}")
        print(f"工具调用次数:  {tool_call_count}")

        for role, count in sorted(role_counts.items()):
            role_icons = {"system": "⚙️", "user": "👤", "assistant": "🤖", "tool": "🔧"}
            icon = role_icons.get(role, "❓")
            print(f"{icon} {role:12s} {count}")

        if time_range:
            print(f"\n时间范围: {time_range}")
        print("="*80 + "\n")

        # 可用工具列表
        if tools:
            print("="*80)
            print(f"🛠️  可用工具 ({len(tools)})")
            print("="*80)
            for i, tool in enumerate(tools, 1):
                tool_name = tool.get("function", {}).get("name", "unknown")
                tool_desc = tool.get("function", {}).get("description", "")
                if len(tool_desc) > 100:
                    tool_desc = tool_desc[:100] + "..."
                print(f"{i:2d}. {tool_name}")
                if tool_desc:
                    print(f"    {tool_desc}")
            print("="*80 + "\n")


def view_trajectory(
    filepath: Path,
    limit: int = None,
    offset: int = 0,
    show_summary: bool = True,
    use_rich: bool = True,
    roles_filter: List[str] = None
):
    """查看 trajectory 文件"""
    # 读取文件
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        print(f"✗ 无法读取文件: {e}")
        sys.exit(1)

    trajectory = data.get("trajectory", [])

    if not trajectory:
        print("⚠️  Trajectory 为空")
        return

    # 角色过滤
    if roles_filter:
        trajectory = [msg for msg in trajectory if msg.get("role") in roles_filter]

    # 显示摘要
    if show_summary:
        print_summary(data, use_rich)

    # 应用偏移和限制
    total = len(trajectory)
    if offset >= total:
        print(f"⚠️  偏移量 {offset} 超出范围 (总共 {total} 条)")
        return

    end_idx = min(offset + limit, total) if limit else total
    display_trajectory = trajectory[offset:end_idx]

    # 显示消息
    console = None
    if use_rich:
        try:
            from rich.console import Console
            console = Console()
        except ImportError:
            use_rich = False
            print("💡 提示: 安装 rich 库可获得更好的可视化效果: pip install rich")

    timeline: List[Dict[str, Any]] = []
    prev_ts: Optional[str] = None
    for i, msg in enumerate(display_trajectory, start=offset + 1):
        curr_ts = msg.get("timestamp")
        model = get_message_model(msg)
        duration = compute_duration(prev_ts, curr_ts)
        timeline.append(
            {
                "index": i,
                "role": msg.get("role", "unknown"),
                "duration": duration,
            }
        )
        if use_rich and console:
            print_message_rich(
                msg,
                i,
                total,
                console,
                model=model,
                duration=duration
            )
        else:
            print_message_simple(
                msg,
                i,
                total,
                model=model,
                duration=duration
            )
        if curr_ts:
            prev_ts = curr_ts

    # 显示整体耗时成分条（只在 rich 模式下展示）
    if use_rich and console and timeline:
        from rich.panel import Panel
        from rich.text import Text
        from rich.console import Group

        # 只统计非 user 的时间（machine time），排除人工等待时间
        total_duration = sum(
            d["duration"] for d in timeline
            if d.get("duration") is not None and d["duration"] > 0 and d.get("role") != "user"
        )
        if total_duration > 0:
            bar_width = 50
            role_colors = {
                "system": "cyan",
                "user": "green",
                "assistant": "blue",
                "tool": "yellow",
            }

            bar = Text()
            legend = Text()
            for item in timeline:
                duration = item.get("duration")
                role = item.get("role", "unknown")
                idx = item.get("index")
                # 跳过 user 角色的时间统计
                if not duration or duration <= 0 or role == "user":
                    continue
                seg_len = max(1, int(duration / total_duration * bar_width))
                color = role_colors.get(role, "white")
                bar.append("█" * seg_len, style=color)
                legend.append(
                    f"[{idx}] {role} {duration:.3f}s  ",
                    style=color,
                )

            if bar:
                console.print()
                console.print(
                    Panel(
                        Group(
                            Text("各步骤在总耗时中的占比：", style="bold"),
                            bar,
                            Text("", style="dim"),
                            Text("图例（按顺序）：", style="bold"),
                            legend,
                        ),
                        title="⏱️ 时间成分条",
                        border_style="magenta",
                        expand=False,
                    )
                )

    # 显示分页提示
    if end_idx < total:
        remaining = total - end_idx
        print(f"\n{'='*80}")
        print(f"💡 还有 {remaining} 条消息未显示")
        print(f"   使用 --offset {end_idx} 查看后续内容")
        print(f"{'='*80}\n")


def main():
    parser = argparse.ArgumentParser(
        description="可视化展示 Agent Trajectory",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 列出所有 trajectory 文件
  python scripts/view_trajectory.py --list

  # 查看最新的 trajectory
  python scripts/view_trajectory.py --latest

  # 查看第 1 个 trajectory
  python scripts/view_trajectory.py --index 1

  # 查看指定文件
  python scripts/view_trajectory.py path/to/trajectory.json

  # 只显示前 10 条消息
  python scripts/view_trajectory.py --latest --limit 10

  # 从第 20 条开始显示
  python scripts/view_trajectory.py --latest --offset 20 --limit 10

  # 只显示 assistant 和 user 消息
  python scripts/view_trajectory.py --latest --role assistant --role user

  # 不使用彩色输出
  python scripts/view_trajectory.py --latest --no-rich
        """
    )

    parser.add_argument(
        "trajectory_file",
        type=str,
        nargs="?",
        help="Trajectory JSON 文件路径"
    )

    parser.add_argument(
        "--list", "-l",
        action="store_true",
        help="列出所有 trajectory 文件"
    )

    parser.add_argument(
        "--latest",
        action="store_true",
        help="查看最新的 trajectory 文件"
    )

    parser.add_argument(
        "--index", "-i",
        type=int,
        help="查看第 N 个 trajectory 文件（按修改时间倒序）"
    )

    parser.add_argument(
        "--limit", "-n",
        type=int,
        help="显示消息数量限制"
    )

    parser.add_argument(
        "--offset", "-o",
        type=int,
        default=0,
        help="从第几条消息开始显示（默认: 0）"
    )

    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="不显示摘要统计"
    )

    parser.add_argument(
        "--no-rich",
        action="store_true",
        help="不使用 Rich 库的彩色输出"
    )

    parser.add_argument(
        "--role", "-r",
        action="append",
        dest="roles",
        help="只显示指定角色的消息（可多次使用）"
    )

    args = parser.parse_args()

    trajectory_dir = get_trajectory_dir()
    use_rich = not args.no_rich

    # 列出所有文件
    if args.list:
        files = list_trajectory_files(trajectory_dir)
        print_trajectory_list(files, use_rich=use_rich)
        return

    # 确定要查看的文件
    filepath = None

    if args.latest:
        files = list_trajectory_files(trajectory_dir, limit=1)
        if not files:
            print("✗ 未找到 trajectory 文件")
            sys.exit(1)

        filepath = files[0][0]
        print(f"📂 查看最新文件: {filepath.name}\n")

    elif args.index is not None:
        files = list_trajectory_files(trajectory_dir)
        if args.index < 1 or args.index > len(files):
            print(f"✗ 索引超出范围: {args.index} (共 {len(files)} 个文件)")
            sys.exit(1)

        filepath = files[args.index - 1][0]
        print(f"📂 查看第 {args.index} 个文件: {filepath.name}\n")

    elif args.trajectory_file:
        filepath = Path(args.trajectory_file)
        if not filepath.exists():
            print(f"✗ 文件不存在: {filepath}")
            sys.exit(1)

    else:
        parser.print_help()
        print("\n💡 提示: 使用 --list 查看所有 trajectory 文件")
        print("💡 提示: 使用 --latest 查看最新的 trajectory")
        sys.exit(0)

    # 查看 trajectory
    view_trajectory(
        filepath=filepath,
        limit=args.limit,
        offset=args.offset,
        show_summary=not args.no_summary,
        use_rich=use_rich,
        roles_filter=args.roles
    )


if __name__ == "__main__":
    main()
