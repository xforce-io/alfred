#!/usr/bin/env python3
"""
定时任务调度器
每日15:00自动推送
"""

import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path

try:
    import schedule
except ImportError:
    print("Error: schedule not installed")
    print("Run: pip install schedule")
    sys.exit(1)


# 技能根目录
SKILL_DIR = Path(__file__).parent.parent
SCRIPTS_DIR = SKILL_DIR / 'scripts'


def push_daily_case():
    """生成并推送每日案例"""
    print(f"[{datetime.now()}] 开始执行每日推送...")
    
    # 1. 生成案例
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / 'main.py')],
        capture_output=True,
        text=True
    )
    
    if result.returncode != 0:
        print(f"生成案例失败: {result.stderr}")
        return
    
    case_content = result.stdout
    print(f"案例生成成功，长度: {len(case_content)}")
    
    # 2. 推送到Telegram
    push_result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / 'push_telegram.py')],
        input=case_content,
        capture_output=True,
        text=True
    )
    
    if push_result.returncode == 0:
        print(f"[{datetime.now()}] 推送成功!")
    else:
        print(f"推送失败: {push_result.stderr}")


def run_scheduler():
    """运行调度器"""
    push_time = os.getenv('DAILY_ATTRACTOR_TIME', '15:00')
    
    print("🚀 吸引子监控启动...")
    print(f"⏰ 已设置每天 {push_time} 推送")
    print("📱 Telegram推送已配置")
    print("💡 按Ctrl+C停止\n")
    
    # 设置定时任务
    schedule.every().day.at(push_time).do(push_daily_case)
    
    # 立即执行一次（测试）
    # push_daily_case()
    
    # 运行循环
    while True:
        schedule.run_pending()
        import time
        time.sleep(60)


def main():
    """入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Daily Attractor Scheduler')
    parser.add_argument('--run-now', action='store_true', help='立即推送一次')
    parser.add_argument('--time', '-t', default='15:00', help='推送时间 (HH:MM)')
    args = parser.parse_args()
    
    if args.run_now:
        push_daily_case()
    else:
        os.environ['DAILY_ATTRACTOR_TIME'] = args.time
        run_scheduler()


if __name__ == '__main__':
    main()
