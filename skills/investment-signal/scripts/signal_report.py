#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
投资信号综合报告 (Investment Signal Report)

整合宏观流动性监控和美股价值投资分析，生成综合信号报告。

Usage:
    python signal_report.py --all --symbols AAPL,MSFT
    python signal_report.py --macro
    python signal_report.py --value --symbols AAPL,MSFT,GOOGL

Environment:
    FRED_API_KEY - Required for macro liquidity analysis
"""

import sys
import os
import json
import logging
import argparse
from datetime import datetime
from typing import Dict, Any, List

# Add script directory to path for sibling imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from macro_liquidity import MacroLiquidityAnalyzer, _serialize_result, format_text as format_macro_text
from value_investing import ValueInvestingAnalyzer, _make_serializable, format_text_single

logger = logging.getLogger(__name__)


def generate_report(
    include_macro: bool = True,
    include_value: bool = True,
    symbols: List[str] = None,
    lookback_days: int = 365,
) -> Dict[str, Any]:
    """
    生成综合投资信号报告

    Args:
        include_macro: 是否包含宏观流动性分析
        include_value: 是否包含价值投资分析
        symbols: 美股代码列表（价值投资用）
        lookback_days: 宏观流动性回溯天数
    """
    report = {
        'report_type': 'investment_signal',
        'generated_at': datetime.now().isoformat(),
        'sections': {},
    }

    # 宏观流动性
    if include_macro:
        try:
            analyzer = MacroLiquidityAnalyzer()
            macro_result = analyzer.analyze(lookback_days=lookback_days)
            report['sections']['macro_liquidity'] = _serialize_result(macro_result)
        except Exception as e:
            logger.error(f"宏观流动性分析失败: {e}")
            report['sections']['macro_liquidity'] = {'error': str(e)}

    # 价值投资
    if include_value and symbols:
        try:
            analyzer = ValueInvestingAnalyzer()
            value_results = analyzer.scan_value_stocks(symbols)
            report['sections']['value_investing'] = _make_serializable(value_results)
        except Exception as e:
            logger.error(f"价值投资分析失败: {e}")
            report['sections']['value_investing'] = {'error': str(e)}

    # 综合信号汇总
    report['signal_summary'] = _build_signal_summary(report)

    return report


def _build_signal_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    """从各模块提取关键信号汇总"""
    summary = {
        'total_signals': 0,
        'critical_signals': [],  # 🔴
        'warning_signals': [],   # 🟠
        'positive_signals': [],  # 🟢
    }

    # 宏观信号
    macro = report['sections'].get('macro_liquidity', {})
    if 'signals' in macro:
        for sig in macro['signals']:
            summary['total_signals'] += 1
            if '🔴' in sig:
                summary['critical_signals'].append(f'[宏观] {sig}')
            elif '🟠' in sig:
                summary['warning_signals'].append(f'[宏观] {sig}')
            elif '🟢' in sig:
                summary['positive_signals'].append(f'[宏观] {sig}')

    # 价值投资信号
    value = report['sections'].get('value_investing', [])
    if isinstance(value, list):
        for stock in value:
            if isinstance(stock, dict) and 'signals' in stock:
                symbol = stock.get('symbol', '?')
                for sig in stock['signals']:
                    summary['total_signals'] += 1
                    if '❌' in sig:
                        summary['critical_signals'].append(f'[{symbol}] {sig}')
                    elif '⚠️' in sig:
                        summary['warning_signals'].append(f'[{symbol}] {sig}')
                    elif '✅' in sig:
                        summary['positive_signals'].append(f'[{symbol}] {sig}')

    return summary


def format_text_report(report: Dict[str, Any]) -> str:
    """Format full report as human-readable text"""
    lines = []
    lines.append(f"{'='*60}")
    lines.append(f"  投资信号综合报告")
    lines.append(f"  {report['generated_at'][:19]}")
    lines.append(f"{'='*60}")

    # 宏观流动性
    macro = report['sections'].get('macro_liquidity')
    if macro:
        if 'error' in macro:
            lines.append(f"\n  [宏观流动性] ❌ {macro['error']}")
        else:
            lines.append(f"")
            lines.append(format_macro_text(macro))

    # 价值投资
    value = report['sections'].get('value_investing')
    if value:
        if isinstance(value, dict) and 'error' in value:
            lines.append(f"\n  [价值投资] ❌ {value['error']}")
        elif isinstance(value, list):
            lines.append(f"")
            lines.append(f"{'='*60}")
            lines.append(f"  美股价值投资分析")
            lines.append(f"{'='*60}")
            for stock in value:
                lines.append(format_text_single(stock))

    # 信号汇总
    summary = report.get('signal_summary', {})
    lines.append(f"")
    lines.append(f"{'='*60}")
    lines.append(f"  信号汇总 (共 {summary.get('total_signals', 0)} 条)")
    lines.append(f"{'='*60}")

    if summary.get('critical_signals'):
        lines.append(f"")
        lines.append(f"  🔴 关键风险信号:")
        for sig in summary['critical_signals']:
            lines.append(f"    {sig}")

    if summary.get('warning_signals'):
        lines.append(f"")
        lines.append(f"  🟠 警告信号:")
        for sig in summary['warning_signals']:
            lines.append(f"    {sig}")

    if summary.get('positive_signals'):
        lines.append(f"")
        lines.append(f"  🟢 正面信号:")
        for sig in summary['positive_signals']:
            lines.append(f"    {sig}")

    lines.append(f"")
    lines.append(f"{'='*60}")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='投资信号综合报告 — 整合宏观流动性 + 价值投资分析',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--macro', action='store_true', help='仅运行宏观流动性分析')
    parser.add_argument('--value', action='store_true', help='仅运行价值投资分析')
    parser.add_argument('--all', action='store_true', help='运行所有分析模块')
    parser.add_argument('--symbols', type=str, help='美股代码，逗号分隔 (e.g. AAPL,MSFT)')
    parser.add_argument('--lookback-days', type=int, default=365, help='宏观流动性回溯天数')
    parser.add_argument('--format', choices=['json', 'text'], default='text', help='输出格式')
    args = parser.parse_args()

    if not args.macro and not args.value and not args.all:
        parser.error('请指定分析模块: --macro, --value, 或 --all')

    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')

    include_macro = args.macro or args.all
    include_value = args.value or args.all

    symbols = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(',') if s.strip()]
    elif include_value:
        print("提示: 价值投资分析需要指定 --symbols 参数", file=sys.stderr)

    report = generate_report(
        include_macro=include_macro,
        include_value=include_value,
        symbols=symbols,
        lookback_days=args.lookback_days,
    )

    if args.format == 'json':
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_text_report(report))


if __name__ == '__main__':
    main()
