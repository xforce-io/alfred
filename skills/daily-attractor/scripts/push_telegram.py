#!/usr/bin/env python3
"""
Telegram 消息格式化输出脚本（纯 stdout，推送由系统统一处理）

注意：此脚本不再直接调用 Telegram API。
系统层的 TelegramChannel 会捕获 stdout 并推送消息。
"""

import sys


def main():
    """从stdin读取消息并原样输出到stdout（由系统统一推送）"""
    message = sys.stdin.read()
    
    if not message.strip():
        # 兜底：stdin 为空时自己调用 main.py 生成内容
        import subprocess
        from pathlib import Path
        main_script = Path(__file__).parent / "main.py"
        result = subprocess.run(
            [sys.executable, str(main_script)],
            capture_output=True, text=True
        )
        message = result.stdout
        if not message.strip():
            print("Error: No message to output", file=sys.stderr)
            sys.exit(1)
    
    # 直接输出到 stdout，由系统的 TelegramChannel 统一推送
    print(message)
    sys.exit(0)


if __name__ == '__main__':
    main()
