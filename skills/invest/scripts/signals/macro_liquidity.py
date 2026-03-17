#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
宏观流动性监控分析器 (Macro Liquidity Analyzer)

四维度监控，每个维度有明确的触发阈值：

| 维度     | 数据源                           | 预警条件              | 权重 |
|---------|----------------------------------|----------------------|------|
| 净流动性 | FRED: WALCL - WTREGEN - RRPONTSYD | 单周下降 > 5%         | 40%  |
| SOFR    | FRED: SOFR                        | 突破 5.5%            | 25%  |
| MOVE指数 | Yahoo: ^MOVE                      | 超过 130             | 20%  |
| 日元套利 | Yahoo: JPY=X + FRED: DGS2        | USD/JPY急跌+利差收窄   | 15%  |

状态输出：充裕(Abundant) / 正常(Normal) / 偏紧(Tight) / 危机(Crisis)

Usage:
    python macro_liquidity.py [--lookback-days 365] [--format json|text]

Environment:
    FRED_API_KEY - Required. Get free key at https://fred.stlouisfed.org/docs/api/api_key.html
"""

import os
import sys
import json
import logging
import argparse
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Check dependencies
def _check_dependencies():
    missing = []
    try:
        import pandas  # noqa: F401
    except ImportError:
        missing.append('pandas')
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append('numpy')
    try:
        import fredapi  # noqa: F401
    except ImportError:
        missing.append('fredapi')
    try:
        import yfinance  # noqa: F401
    except ImportError:
        missing.append('yfinance')
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}", file=sys.stderr)
        print(f"Install with: pip install {' '.join(missing)}", file=sys.stderr)
        sys.exit(1)

_check_dependencies()

import numpy as np
import pandas as pd

# 阈值配置
THRESHOLDS = {
    'net_liquidity_drop_pct': 5.0,     # 单周净流动性下降超 5%
    'sofr_high': 5.5,                   # SOFR 超过 5.5%
    'move_high': 130,                   # MOVE 指数超 130
    'yen_drop_weekly_pct': 3.0,        # USD/JPY 周跌幅超 3%
    'yield_spread_narrow': 1.0,         # 美日利差收窄至 1% 以下
}

# 维度权重
DIMENSION_WEIGHTS = {
    'net_liquidity': 0.40,
    'sofr': 0.25,
    'move': 0.20,
    'yen_carry': 0.15,
}

# 状态映射 (基于风险分数)
STATUS_MAP = [
    (75, 'Crisis', '危机', '🔴'),
    (50, 'Tight', '偏紧', '🟠'),
    (25, 'Normal', '正常', '🟡'),
    (0, 'Abundant', '充裕', '🟢'),
]


class MacroLiquidityAnalyzer:
    """
    宏观流动性监控分析器

    监控美联储资产负债表净流动性、SOFR、MOVE指数和日元套利交易，
    多条件触发预警。
    """

    def __init__(self, fred_api_key: Optional[str] = None):
        self._fred = None
        self._fred_api_key = fred_api_key

    def _get_fred(self):
        """懒加载 FRED 客户端"""
        if self._fred is None:
            from fredapi import Fred
            api_key = self._fred_api_key or os.environ.get('FRED_API_KEY')
            if not api_key:
                raise ValueError(
                    "需要 FRED API Key，请设置环境变量 FRED_API_KEY 或传入参数。"
                    "免费申请: https://fred.stlouisfed.org/docs/api/api_key.html"
                )
            self._fred = Fred(api_key=api_key)
        return self._fred

    def analyze(self, lookback_days: int = 365) -> Dict[str, Any]:
        """
        全量分析宏观流动性状态

        Args:
            lookback_days: 回溯天数

        Returns:
            分析结果字典，包含四个维度的数据 + 触发信号列表
        """
        signals = []
        dimension_scores = {}
        dimensions = {}

        # 1. 净流动性
        try:
            nl_result = self._fetch_net_liquidity(lookback_days)
            dimensions['net_liquidity'] = nl_result
            dimension_scores['net_liquidity'] = nl_result.get('risk_score', 50)
            signals.extend(nl_result.get('signals', []))
        except Exception as e:
            logger.error(f"获取净流动性数据失败: {e}")
            dimensions['net_liquidity'] = {'error': str(e)}
            dimension_scores['net_liquidity'] = 50

        # 2. SOFR
        try:
            sofr_result = self._fetch_sofr(lookback_days)
            dimensions['sofr'] = sofr_result
            dimension_scores['sofr'] = sofr_result.get('risk_score', 50)
            signals.extend(sofr_result.get('signals', []))
        except Exception as e:
            logger.error(f"获取 SOFR 数据失败: {e}")
            dimensions['sofr'] = {'error': str(e)}
            dimension_scores['sofr'] = 50

        # 3. MOVE 指数
        try:
            move_result = self._fetch_move_index(lookback_days)
            dimensions['move'] = move_result
            dimension_scores['move'] = move_result.get('risk_score', 50)
            signals.extend(move_result.get('signals', []))
        except Exception as e:
            logger.error(f"获取 MOVE 指数失败: {e}")
            dimensions['move'] = {'error': str(e)}
            dimension_scores['move'] = 50

        # 4. 日元套利
        try:
            yen_result = self._fetch_yen_carry(lookback_days)
            dimensions['yen_carry'] = yen_result
            dimension_scores['yen_carry'] = yen_result.get('risk_score', 50)
            signals.extend(yen_result.get('signals', []))
        except Exception as e:
            logger.error(f"获取日元套利数据失败: {e}")
            dimensions['yen_carry'] = {'error': str(e)}
            dimension_scores['yen_carry'] = 50

        # 加权风险分数
        total_risk_score = sum(
            dimension_scores.get(dim, 50) * weight
            for dim, weight in DIMENSION_WEIGHTS.items()
        )
        total_risk_score = round(total_risk_score, 1)

        # 状态判定
        status_en, status_cn, status_icon = 'Normal', '正常', '🟡'
        for threshold, en, cn, icon in STATUS_MAP:
            if total_risk_score >= threshold:
                status_en, status_cn, status_icon = en, cn, icon
                break

        return {
            'status': status_en,
            'status_cn': status_cn,
            'status_icon': status_icon,
            'risk_score': total_risk_score,
            'dimensions': dimensions,
            'dimension_scores': dimension_scores,
            'signals': signals,
            'thresholds': THRESHOLDS,
            'analyzed_at': datetime.now().isoformat(),
        }

    # ==================== 数据获取 ====================

    def _fetch_net_liquidity(self, lookback_days: int = 365) -> Dict[str, Any]:
        """
        获取净流动性数据
        净流动性 = Fed 总资产 (WALCL) - 财政部一般账户 (WTREGEN) - 隔夜逆回购 (RRPONTSYD)
        """
        fred = self._get_fred()
        start = datetime.now() - timedelta(days=lookback_days + 30)

        walcl = fred.get_series('WALCL', observation_start=start)
        tga = fred.get_series('WTREGEN', observation_start=start)
        rrp = fred.get_series('RRPONTSYD', observation_start=start)

        df = pd.DataFrame({'walcl': walcl, 'tga': tga, 'rrp': rrp})
        df = df.asfreq('D').ffill()
        df = df.dropna()

        if df.empty:
            return {'error': '净流动性数据为空', 'risk_score': 50}

        df['net_liquidity'] = df['walcl'] - df['tga'] - df['rrp']

        latest = df.iloc[-1]
        net_liq = latest['net_liquidity']

        if len(df) >= 7:
            week_ago = df.iloc[-7]['net_liquidity']
            weekly_change_pct = (net_liq - week_ago) / abs(week_ago) * 100 if week_ago != 0 else 0
        else:
            weekly_change_pct = 0

        risk_score = 0
        signals = []

        if weekly_change_pct < -THRESHOLDS['net_liquidity_drop_pct']:
            risk_score = 90
            signals.append(f'🔴 净流动性单周暴跌 {weekly_change_pct:.1f}%（阈值 -{THRESHOLDS["net_liquidity_drop_pct"]}%）')
        elif weekly_change_pct < -2:
            risk_score = 60
            signals.append(f'🟠 净流动性周降 {weekly_change_pct:.1f}%')
        elif weekly_change_pct < 0:
            risk_score = 30
        else:
            risk_score = 10
            signals.append(f'🟢 净流动性稳定/增长 ({weekly_change_pct:+.1f}%)')

        return {
            'current': round(net_liq / 1e6, 2),  # 转为万亿
            'current_raw': net_liq,
            'weekly_change_pct': round(weekly_change_pct, 2),
            'components': {
                'fed_assets': round(float(latest['walcl']) / 1e6, 2),
                'tga': round(float(latest['tga']) / 1e6, 2),
                'rrp': round(float(latest['rrp']) / 1e6, 2),
            },
            'risk_score': risk_score,
            'signals': signals,
        }

    def _fetch_sofr(self, lookback_days: int = 365) -> Dict[str, Any]:
        """获取 SOFR 利率数据"""
        fred = self._get_fred()
        start = datetime.now() - timedelta(days=lookback_days + 30)

        sofr = fred.get_series('SOFR', observation_start=start)

        if sofr is None or sofr.empty:
            return {'error': 'SOFR 数据为空', 'risk_score': 50}

        sofr = sofr.dropna()
        current_sofr = float(sofr.iloc[-1])

        risk_score = 0
        signals = []

        if current_sofr >= THRESHOLDS['sofr_high']:
            risk_score = 80
            signals.append(f'🔴 SOFR 突破 {THRESHOLDS["sofr_high"]}%（当前 {current_sofr:.2f}%）')
        elif current_sofr >= 5.0:
            risk_score = 50
            signals.append(f'🟠 SOFR 偏高 ({current_sofr:.2f}%)')
        elif current_sofr >= 4.0:
            risk_score = 25
        else:
            risk_score = 10
            signals.append(f'🟢 SOFR 水平正常 ({current_sofr:.2f}%)')

        return {
            'current': current_sofr,
            'risk_score': risk_score,
            'signals': signals,
        }

    def _fetch_move_index(self, lookback_days: int = 365) -> Dict[str, Any]:
        """获取 MOVE 指数（债市波动率指数）"""
        import yfinance as yf

        end = datetime.now()
        start = end - timedelta(days=lookback_days + 30)

        move = yf.download('^MOVE', start=start, end=end, progress=False)

        if move is None or move.empty:
            return {'error': 'MOVE 指数数据为空', 'risk_score': 50}

        if isinstance(move.columns, pd.MultiIndex):
            move.columns = move.columns.get_level_values(0)

        close_col = 'Close' if 'Close' in move.columns else 'close'
        if close_col not in move.columns:
            return {'error': 'MOVE 数据缺少 Close 列', 'risk_score': 50}

        current_move = float(move[close_col].iloc[-1])

        risk_score = 0
        signals = []

        if current_move >= THRESHOLDS['move_high']:
            risk_score = 80
            signals.append(f'🔴 MOVE 指数超 {THRESHOLDS["move_high"]}（当前 {current_move:.1f}）')
        elif current_move >= 110:
            risk_score = 50
            signals.append(f'🟠 MOVE 指数偏高 ({current_move:.1f})')
        elif current_move >= 90:
            risk_score = 25
        else:
            risk_score = 10
            signals.append(f'🟢 MOVE 指数平稳 ({current_move:.1f})')

        return {
            'current': round(current_move, 1),
            'risk_score': risk_score,
            'signals': signals,
        }

    def _fetch_yen_carry(self, lookback_days: int = 365) -> Dict[str, Any]:
        """
        获取日元套利交易数据
        监控 USD/JPY 汇率 + 美日利差 (US 2Y - JP 2Y)
        """
        import yfinance as yf

        end = datetime.now()
        start = end - timedelta(days=lookback_days + 30)

        usdjpy = yf.download('JPY=X', start=start, end=end, progress=False)

        if usdjpy is None or usdjpy.empty:
            return {'error': 'USD/JPY 数据为空', 'risk_score': 50}

        if isinstance(usdjpy.columns, pd.MultiIndex):
            usdjpy.columns = usdjpy.columns.get_level_values(0)

        close_col = 'Close' if 'Close' in usdjpy.columns else 'close'
        usdjpy_series = usdjpy[close_col]
        current_usdjpy = float(usdjpy_series.iloc[-1])

        if len(usdjpy_series) >= 5:
            week_ago_jpy = float(usdjpy_series.iloc[-5])
            jpy_weekly_change = (current_usdjpy - week_ago_jpy) / week_ago_jpy * 100
        else:
            jpy_weekly_change = 0

        us_2y = None
        yield_spread = None
        try:
            fred = self._get_fred()
            dgs2 = fred.get_series('DGS2', observation_start=start)
            if dgs2 is not None and not dgs2.empty:
                dgs2 = dgs2.dropna()
                us_2y = float(dgs2.iloc[-1])
                yield_spread = us_2y  # 日本 2Y 近似 0
        except Exception as e:
            logger.warning(f"获取 DGS2 失败: {e}")

        risk_score = 0
        signals = []

        if jpy_weekly_change < -THRESHOLDS['yen_drop_weekly_pct']:
            risk_score += 50
            signals.append(f'🔴 USD/JPY 周跌 {jpy_weekly_change:.1f}%（日元套利平仓风险）')
        elif jpy_weekly_change < -1:
            risk_score += 25
            signals.append(f'🟠 USD/JPY 走弱 ({jpy_weekly_change:+.1f}%)')
        else:
            signals.append(f'🟢 USD/JPY 稳定 ({current_usdjpy:.1f}, 周变化{jpy_weekly_change:+.1f}%)')

        if yield_spread is not None:
            if yield_spread < THRESHOLDS['yield_spread_narrow']:
                risk_score += 40
                signals.append(f'🔴 美日利差收窄至 {yield_spread:.2f}%（套利动机减弱）')
            elif yield_spread < 2.0:
                risk_score += 20
                signals.append(f'🟠 美日利差 {yield_spread:.2f}%')
            else:
                signals.append(f'🟢 美日利差充足 ({yield_spread:.2f}%)')

        risk_score = min(risk_score, 100)

        return {
            'current_usdjpy': round(current_usdjpy, 2),
            'jpy_weekly_change': round(jpy_weekly_change, 2),
            'us_2y': round(us_2y, 2) if us_2y is not None else None,
            'yield_spread': round(yield_spread, 2) if yield_spread is not None else None,
            'risk_score': risk_score,
            'signals': signals,
        }


def _serialize_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Remove non-serializable fields (like DataFrame series) for JSON output"""
    cleaned = {}
    for k, v in result.items():
        if k == 'series':
            continue
        if isinstance(v, dict):
            cleaned[k] = _serialize_result(v)
        elif isinstance(v, (list, tuple)):
            cleaned[k] = [
                _serialize_result(item) if isinstance(item, dict) else item
                for item in v
            ]
        elif isinstance(v, (np.integer,)):
            cleaned[k] = int(v)
        elif isinstance(v, (np.floating,)):
            cleaned[k] = float(v)
        else:
            cleaned[k] = v
    return cleaned


def format_text(result: Dict[str, Any]) -> str:
    """Format result as human-readable text"""
    lines = []
    lines.append(f"{'='*60}")
    lines.append("  宏观流动性监控报告")
    lines.append(f"  {result['analyzed_at'][:19]}")
    lines.append(f"{'='*60}")
    lines.append("")
    lines.append(f"  综合状态: {result['status_icon']} {result['status']} ({result['status_cn']})")
    lines.append(f"  风险评分: {result['risk_score']}/100")
    lines.append("")

    # 维度评分
    lines.append(f"  {'─'*50}")
    lines.append("  维度评分:")
    dim_labels = {
        'net_liquidity': '净流动性 (40%)',
        'sofr': 'SOFR     (25%)',
        'move': 'MOVE指数  (20%)',
        'yen_carry': '日元套利  (15%)',
    }
    for dim, label in dim_labels.items():
        score = result['dimension_scores'].get(dim, '?')
        dim_data = result['dimensions'].get(dim, {})
        if 'error' in dim_data:
            lines.append(f"    {label}: ⚠️  {dim_data['error']}")
        else:
            lines.append(f"    {label}: {score}/100")
            # Add key metrics
            if dim == 'net_liquidity' and 'current' in dim_data:
                lines.append(f"      净流动性: {dim_data['current']}T, 周变化: {dim_data.get('weekly_change_pct', '?')}%")
                comp = dim_data.get('components', {})
                if comp:
                    lines.append(f"      Fed资产: {comp.get('fed_assets', '?')}T | TGA: {comp.get('tga', '?')}T | RRP: {comp.get('rrp', '?')}T")
            elif dim == 'sofr' and 'current' in dim_data:
                lines.append(f"      当前 SOFR: {dim_data['current']:.2f}%")
            elif dim == 'move' and 'current' in dim_data:
                lines.append(f"      当前 MOVE: {dim_data['current']}")
            elif dim == 'yen_carry':
                if 'current_usdjpy' in dim_data:
                    lines.append(f"      USD/JPY: {dim_data['current_usdjpy']}, 周变化: {dim_data.get('jpy_weekly_change', '?')}%")
                if dim_data.get('yield_spread') is not None:
                    lines.append(f"      美日利差: {dim_data['yield_spread']}%")

    # 信号
    lines.append("")
    lines.append(f"  {'─'*50}")
    lines.append("  触发信号:")
    if result['signals']:
        for sig in result['signals']:
            lines.append(f"    {sig}")
    else:
        lines.append("    无触发信号")

    lines.append("")
    lines.append(f"{'='*60}")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='宏观流动性监控分析器 — 四维度风险评分',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='环境变量: FRED_API_KEY (必需)\n申请地址: https://fred.stlouisfed.org/docs/api/api_key.html'
    )
    parser.add_argument('--lookback-days', type=int, default=365, help='回溯天数 (默认 365)')
    parser.add_argument('--format', choices=['json', 'text'], default='text', help='输出格式 (默认 text)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')

    analyzer = MacroLiquidityAnalyzer()
    result = analyzer.analyze(lookback_days=args.lookback_days)

    if args.format == 'json':
        print(json.dumps(_serialize_result(result), ensure_ascii=False, indent=2))
    else:
        print(format_text(_serialize_result(result)))


if __name__ == '__main__':
    main()
