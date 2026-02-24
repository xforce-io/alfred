#!/usr/bin/env python3
"""
Telegram 推送脚本
"""

import os
import sys
import asyncio
from datetime import datetime

try:
    from telegram import Bot
    from telegram.error import TelegramError
except ImportError:
    print("Error: python-telegram-bot not installed")
    print("Run: pip install python-telegram-bot")
    sys.exit(1)


def load_config():
    """加载配置"""
    token = os.getenv('TELEGRAM_TOKEN') or os.getenv('DAILY_ATTRACTOR_TELEGRAM_TOKEN')
    chat_id = os.getenv('TELEGRAM_CHAT_ID') or os.getenv('DAILY_ATTRACTOR_CHAT_ID')
    
    if not token or not chat_id:
        # 尝试从配置文件读取
        config_paths = [
            os.path.expanduser('~/.alfred/config/dolphin.yaml'),
            os.path.expanduser('~/.config/alfred/dolphin.yaml'),
        ]
        for path in config_paths:
            if os.path.exists(path):
                import yaml
                with open(path, 'r') as f:
                    config = yaml.safe_load(f)
                    if 'daily_attractor' in config:
                        token = token or config['daily_attractor'].get('telegram_token')
                        chat_id = chat_id or config['daily_attractor'].get('chat_id')
                        break
    
    return token, chat_id


async def send_message(message: str, token: str = None, chat_id: str = None):
    """发送消息"""
    token = token or load_config()[0]
    chat_id = chat_id or load_config()[1]
    
    if not token or not chat_id:
        print("Error: TELEGRAM_TOKEN and TELEGRAM_CHAT_ID required")
        print("Set environment variables or config in dolphin.yaml")
        return False
    
    try:
        bot = Bot(token=token)
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode='Markdown',
            disable_web_page_preview=True
        )
        print(f"[{datetime.now()}] 推送成功")
        return True
    except TelegramError as e:
        print(f"推送失败: {e}")
        return False


def main():
    """从stdin读取消息并推送"""
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument('--message', '-m', help='Message to send')
    parser.add_argument('--file', '-f', help='Read message from file')
    args = parser.parse_args()
    
    if args.file:
        with open(args.file, 'r') as f:
            message = f.read()
    elif args.message:
        message = args.message
    else:
        # 从stdin读取
        message = sys.stdin.read()
    
    if not message.strip():
        print("Error: No message to send")
        sys.exit(1)
    
    success = asyncio.run(send_message(message))
    sys.exit(0 if success else 1)


if __name__ == '__main__':
    main()
