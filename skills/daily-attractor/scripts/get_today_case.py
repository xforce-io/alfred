#!/usr/bin/env python3
"""
获取今日案例 - 供Alfred调用
"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from main import AttractorGenerator


def main():
    """CLI入口，供Alfred直接调用"""
    config_path = None
    
    # 查找配置文件
    config_candidates = [
        os.path.expanduser('~/.alfred/skills/daily-attractor/config/watchlist.json'),
        os.path.expanduser('~/.config/daily-attractor/watchlist.json'),
    ]
    for path in config_candidates:
        if os.path.exists(path):
            config_path = path
            break
    
    # 生成并输出
    generator = AttractorGenerator(config_path)
    report = generator.get_today_case()
    print(report)


if __name__ == '__main__':
    main()
