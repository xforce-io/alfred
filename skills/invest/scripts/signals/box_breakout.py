#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
箱体突破分析器 (Box Breakout Analyzer)

基于唐奇安通道的箱体突破检测与评分模型。

核心逻辑：
1. 使用 Donchian Channel 计算箱体上下轨（前一日值避免自我突破）
2. 检测收盘价是否突破箱体 + 成交量放大确认
3. 三因子加权评分：突破强度(40%) + 量能放大(30%) + 箱体紧度(30%)

适用场景：
- 从候选池中筛选出突破箱体整理区间的股票
- 配合量能确认，过滤假突破

Usage:
    python box_breakout.py <SYMBOL> [--format json|text]
    python box_breakout.py --symbols AAPL,MSFT [--format json|text]
    python box_breakout.py --symbols 600519.SH --provider tushare [--format json|text]

Environment:
    TUSHARE_TOKEN - Optional. Required only when using --provider tushare
"""

import os
import sys
import json
import logging
import argparse
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    if missing:
        print(f"Missing dependencies: {', '.join(missing)}", file=sys.stderr)
        print(f"Install with: pip install {' '.join(missing)}", file=sys.stderr)
        sys.exit(1)


_check_dependencies()

import numpy as np
import pandas as pd


# ==================== Inlined utility functions ====================

def _calculate_donchian_channels(data: pd.DataFrame, period: int = 20) -> Dict[str, pd.Series]:
    """
    计算唐奇安通道

    Args:
        data: 包含 high, low 列的 DataFrame
        period: 计算周期

    Returns:
        {'Donchian_High': Series, 'Donchian_Low': Series}
    """
    required_cols = ['high', 'low']
    if not all(col in data.columns for col in required_cols) or len(data) < period:
        empty_series = pd.Series(index=data.index, dtype=float)
        return {
            'Donchian_High': empty_series,
            'Donchian_Low': empty_series,
        }

    donchian_high = data['high'].rolling(window=period).max()
    donchian_low = data['low'].rolling(window=period).min()

    return {
        'Donchian_High': donchian_high,
        'Donchian_Low': donchian_low,
    }


def _normalize_score(value: float, min_val: float, max_val: float,
                     inverse: bool = False) -> float:
    """
    将原始值标准化到 0-100 范围

    Args:
        value: 原始值
        min_val: 最小值
        max_val: 最大值
        inverse: 是否反转（True 表示越小越好）

    Returns:
        0-100 的标准化分数
    """
    if max_val == min_val:
        return 50.0

    normalized = (value - min_val) / (max_val - min_val) * 100
    normalized = max(0, min(100, normalized))

    if inverse:
        normalized = 100 - normalized

    return normalized


# ==================== Data fetching ====================

def _fetch_yfinance(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Fetch OHLCV data via yfinance"""
    try:
        import yfinance as yf
    except ImportError:
        print("Missing dependency: yfinance", file=sys.stderr)
        print("Install with: pip install yfinance", file=sys.stderr)
        sys.exit(1)

    end = datetime.now()
    start = end - timedelta(days=days * 2)

    df = yf.download(symbol, start=start, end=end, progress=False)
    if df is None or df.empty:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    # Normalize column names to lowercase
    df.columns = [c.lower() for c in df.columns]

    return df.tail(days)


def _fetch_tushare(symbol: str, days: int) -> Optional[pd.DataFrame]:
    """Fetch OHLCV data via tushare"""
    try:
        import tushare as ts
    except ImportError:
        print("Missing dependency: tushare", file=sys.stderr)
        print("Install with: pip install tushare", file=sys.stderr)
        sys.exit(1)

    token = os.environ.get('TUSHARE_TOKEN')
    if not token:
        raise ValueError("TUSHARE_TOKEN environment variable is required for tushare provider")

    pro = ts.pro_api(token)
    end = datetime.now().strftime('%Y%m%d')
    start = (datetime.now() - timedelta(days=days * 2)).strftime('%Y%m%d')

    df = pro.daily(ts_code=symbol, start_date=start, end_date=end)
    if df is None or df.empty:
        return None

    df = df.sort_values('trade_date')

    # Normalize column names
    col_map = {
        'open': 'open', 'high': 'high', 'low': 'low',
        'close': 'close', 'vol': 'volume', 'amount': 'amount',
    }
    df = df.rename(columns=col_map)

    return df.tail(days)


def _fetch_data(symbol: str, days: int, provider: str = 'yfinance') -> Optional[pd.DataFrame]:
    """Fetch OHLCV data from the specified provider"""
    if provider == 'tushare':
        return _fetch_tushare(symbol, days)
    else:
        return _fetch_yfinance(symbol, days)


# ==================== Analyzer ====================

class BoxBreakoutAnalyzer:
    """
    箱体突破分析器

    基于唐奇安通道的箱体突破检测，支持批量扫描。
    """

    def __init__(self, provider: str = 'yfinance'):
        """
        Args:
            provider: 数据源 ('yfinance' for US stocks, 'tushare' for A-shares)
        """
        self.provider = provider

    def analyze(self, symbol: str, period: int = 20, days: int = 120,
                volume_threshold: float = 1.5) -> Dict:
        """
        分析单个标的的箱体突破状态

        Args:
            symbol: 标的代码
            period: 箱体计算周期（天）
            days: 获取的历史数据天数
            volume_threshold: 放量确认倍数阈值

        Returns:
            分析结果字典
        """
        try:
            data = _fetch_data(symbol, days, self.provider)
            if data is None or len(data) < period + 5:
                actual = len(data) if data is not None else 0
                return {
                    'symbol': symbol,
                    'error': f'数据不足，需要至少{period + 5}个交易日（当前:{actual}）'
                }

            return self._analyze_dataframe(data, symbol, period, volume_threshold)

        except Exception as e:
            logger.error(f"分析 {symbol} 箱体突破失败: {e}")
            return {'symbol': symbol, 'error': str(e)}

    def _analyze_dataframe(self, df: pd.DataFrame, symbol: str = "unknown",
                           period: int = 20, volume_threshold: float = 1.5) -> Dict:
        """从 DataFrame 分析箱体突破（核心逻辑）"""
        required_cols = ['high', 'low', 'close', 'volume']
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            return {'symbol': symbol, 'error': f'数据缺少必要列: {missing}'}

        if len(df) < period + 5:
            return {
                'symbol': symbol,
                'error': f'数据不足，需要至少{period + 5}个交易日（当前:{len(df)}）'
            }

        data = df.copy()

        # === Step 1: 计算箱体（唐奇安通道） ===
        donchian = _calculate_donchian_channels(data, period)
        data['box_high'] = donchian['Donchian_High'].shift(1)  # 前一日值，避免自我突破
        data['box_low'] = donchian['Donchian_Low'].shift(1)

        # === Step 2: 计算量能指标 ===
        data['avg_volume'] = data['volume'].rolling(window=period).mean()
        data['volume_ratio'] = data['volume'] / data['avg_volume']

        # 取最新一行进行判断
        latest = data.iloc[-1]

        box_high = latest['box_high']
        box_low = latest['box_low']
        close = latest['close']
        volume_ratio = latest['volume_ratio']

        if pd.isna(box_high) or pd.isna(box_low) or pd.isna(volume_ratio):
            return {'symbol': symbol, 'error': '指标计算结果含空值，数据可能不足'}

        box_range_pct = (box_high - box_low) / box_low * 100 if box_low > 0 else 0

        # === Step 3: 检测突破（仅基于价格位置） ===
        breakout_type = 'none'
        breakout_pct = 0.0
        volume_confirmed = volume_ratio >= volume_threshold

        if close > box_high:
            breakout_type = 'up'
            breakout_pct = (close - box_high) / box_high * 100
        elif close < box_low:
            breakout_type = 'down'
            breakout_pct = (box_low - close) / box_low * 100

        # === Step 4: 三因子评分 (0-100) ===
        if breakout_type == 'none':
            # 箱体内：计算距离上轨的接近程度作为参考分
            if box_high > box_low:
                proximity = (close - box_low) / (box_high - box_low)
            else:
                proximity = 0.5
            strength_score = proximity * 30
            volume_score = _normalize_score(volume_ratio, min_val=0.5, max_val=3.0)
            tightness_score = _normalize_score(box_range_pct, min_val=0, max_val=30, inverse=True)
            score = round(
                strength_score * 0.4 + volume_score * 0.3 + tightness_score * 0.3, 1
            )
            score = min(score, 30.0)
        else:
            # 因子1: 突破强度 (40%)
            strength_score = _normalize_score(breakout_pct, min_val=0, max_val=10)
            # 因子2: 量能放大 (30%)
            volume_score = _normalize_score(volume_ratio, min_val=0.5, max_val=5.0)
            # 因子3: 箱体紧度 (30%)
            tightness_score = _normalize_score(box_range_pct, min_val=0, max_val=30, inverse=True)

            score = round(
                strength_score * 0.4 + volume_score * 0.3 + tightness_score * 0.3, 1
            )
            # 未放量确认的突破打折（×0.7）
            if not volume_confirmed:
                score = round(score * 0.7, 1)

        # === Step 5: 确定等级和建议 ===
        level, level_icon = self._determine_level(score, breakout_type, volume_confirmed)
        signals = self._generate_signals(breakout_type, breakout_pct, volume_ratio,
                                         box_range_pct, box_high, box_low,
                                         volume_confirmed, volume_threshold)
        recommendation = self._generate_recommendation(score, breakout_type, volume_confirmed)

        return {
            'symbol': symbol,
            'score': score,
            'breakout_type': breakout_type,
            'volume_confirmed': volume_confirmed,
            'box_high': round(float(box_high), 2),
            'box_low': round(float(box_low), 2),
            'box_range_pct': round(float(box_range_pct), 2),
            'latest_close': round(float(close), 2),
            'breakout_pct': round(float(breakout_pct), 2),
            'volume_ratio': round(float(volume_ratio), 2),
            'details': {
                'strength_score': round(strength_score, 1),
                'volume_score': round(volume_score, 1),
                'tightness_score': round(tightness_score, 1),
            },
            'signals': signals,
            'recommendation': recommendation,
            'level': level,
            'level_icon': level_icon,
            'data_points': len(data),
        }

    def scan_breakouts(self, symbols: List[str], period: int = 20, days: int = 120,
                       volume_threshold: float = 1.5, max_workers: int = 3) -> List[Dict]:
        """批量扫描箱体突破（并发执行）"""
        def _analyze_one(symbol):
            return self.analyze(symbol, period, days, volume_threshold)

        results = []
        workers = min(max_workers, len(symbols))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(_analyze_one, s): s for s in symbols}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    sym = futures[future]
                    logger.error(f"扫描 {sym} 异常: {e}")
                    results.append({'symbol': sym, 'error': str(e)})

        results.sort(key=lambda x: x.get('score', -1), reverse=True)
        return results

    @staticmethod
    def _determine_level(score: float, breakout_type: str, volume_confirmed: bool = True):
        """根据得分和突破类型确定等级"""
        if breakout_type == 'none':
            if score >= 20:
                return ('临近突破', '⏳')
            return ('箱体内', '📦')

        vol_tag = "" if volume_confirmed else "(缩量)"

        if breakout_type == 'up':
            if score >= 70:
                return (f'强势突破{vol_tag}', '🚀')
            elif score >= 50:
                return (f'有效突破{vol_tag}', '📈')
            elif score >= 30:
                return (f'弱突破{vol_tag}', '↗️')
            else:
                return (f'勉强突破{vol_tag}', '➡️')
        else:  # down
            if score >= 70:
                return (f'强势破位{vol_tag}', '💥')
            elif score >= 50:
                return (f'有效破位{vol_tag}', '📉')
            elif score >= 30:
                return (f'弱破位{vol_tag}', '↘️')
            else:
                return (f'勉强破位{vol_tag}', '➡️')

    @staticmethod
    def _generate_signals(breakout_type, breakout_pct, volume_ratio,
                          box_range_pct, box_high, box_low,
                          volume_confirmed=True, volume_threshold=1.5):
        """生成信号文字描述"""
        signals = []
        if breakout_type == 'up':
            signals.append(f"📈 向上突破箱体上轨 {box_high:.2f}，突破幅度 {breakout_pct:.2f}%")
        elif breakout_type == 'down':
            signals.append(f"📉 向下突破箱体下轨 {box_low:.2f}，突破幅度 {breakout_pct:.2f}%")
        else:
            signals.append(f"📦 价格在箱体内运行 ({box_low:.2f} ~ {box_high:.2f})")

        vol_status = "放量" if volume_confirmed else f"缩量(未达{volume_threshold:.1f}x)"
        signals.append(f"📊 成交量为均量的 {volume_ratio:.2f} 倍 — {vol_status}")
        signals.append(f"📐 箱体宽度 {box_range_pct:.2f}%")
        return signals

    @staticmethod
    def _generate_recommendation(score, breakout_type, volume_confirmed=True):
        """生成综合建议"""
        if breakout_type == 'none':
            if score >= 20:
                return "⏳ 价格接近箱体上轨，关注是否放量突破"
            return "📦 无突破信号，价格在箱体内运行，可关注后续方向选择"

        vol_warn = "" if volume_confirmed else "（注意：未放量确认，需观察后续量能）"

        if breakout_type == 'up':
            if score >= 70:
                return f"🟢 强势突破，可积极跟进，注意回踩确认{vol_warn}"
            elif score >= 50:
                return f"🟡 有效突破，可适度参与，关注量能持续性{vol_warn}"
            elif score >= 30:
                return f"⚪ 突破力度一般，建议等待回踩确认后再介入{vol_warn}"
            else:
                return f"⚠️ 勉强突破，箱体过宽或幅度不足{vol_warn}"
        else:
            if score >= 70:
                return f"🔴 强势破位，建议果断止损或回避{vol_warn}"
            elif score >= 50:
                return f"🟠 有效破位，建议减仓控制风险{vol_warn}"
            elif score >= 30:
                return f"⚪ 弱破位，可观察是否为假跌破{vol_warn}"
            else:
                return f"⚠️ 勉强破位，可能是假跌破{vol_warn}"


def _make_serializable(obj):
    """Convert numpy types for JSON serialization"""
    if isinstance(obj, dict):
        return {k: _make_serializable(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [_make_serializable(item) for item in obj]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, np.bool_):
        return bool(obj)
    elif isinstance(obj, pd.DataFrame):
        return None
    else:
        return obj


def format_text_single(result: Dict[str, Any]) -> str:
    """Format single stock breakout result as text"""
    if 'error' in result:
        return f"  {result['symbol']}: ❌ {result['error']}"

    lines = []
    lines.append(f"  {'─'*55}")
    lines.append(f"  {result['symbol']}")
    lines.append(f"  突破状态: {result['level_icon']} {result['level']} (评分: {result['score']})")
    lines.append(f"  最新价格: {result['latest_close']}")
    lines.append(f"  箱体区间: {result['box_low']} ~ {result['box_high']} (宽度: {result['box_range_pct']}%)")
    lines.append(f"  突破幅度: {result['breakout_pct']}%")
    lines.append(f"  量比: {result['volume_ratio']}x")
    lines.append("")
    lines.append("  评分明细:")
    details = result['details']
    lines.append(f"    突破强度(40%): {details['strength_score']}")
    lines.append(f"    量能放大(30%): {details['volume_score']}")
    lines.append(f"    箱体紧度(30%): {details['tightness_score']}")
    lines.append("")
    lines.append("  信号:")
    for sig in result.get('signals', []):
        lines.append(f"    {sig}")
    lines.append("")
    lines.append(f"  建议: {result.get('recommendation', '')}")
    return '\n'.join(lines)


def format_text(results: List[Dict[str, Any]]) -> str:
    """Format breakout results as human-readable text"""
    lines = []
    lines.append(f"{'='*60}")
    lines.append("  箱体突破分析报告")
    lines.append(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"{'='*60}")

    for result in results:
        lines.append(format_text_single(result))

    lines.append(f"{'='*60}")
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(
        description='箱体突破分析器 — 基于唐奇安通道的突破检测与评分',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('symbol', nargs='?', help='标的代码 (e.g. AAPL, 600519.SH)')
    parser.add_argument('--symbols', type=str, help='批量分析，逗号分隔')
    parser.add_argument('--provider', choices=['yfinance', 'tushare'], default='yfinance',
                        help='数据源 (默认 yfinance)')
    parser.add_argument('--period', type=int, default=20, help='箱体计算周期 (默认 20)')
    parser.add_argument('--days', type=int, default=120, help='历史数据天数 (默认 120)')
    parser.add_argument('--volume-threshold', type=float, default=1.5,
                        help='放量确认倍数 (默认 1.5)')
    parser.add_argument('--format', choices=['json', 'text'], default='text',
                        help='输出格式 (默认 text)')
    args = parser.parse_args()

    if not args.symbol and not args.symbols:
        parser.error('请提供标的代码: python box_breakout.py AAPL 或 --symbols AAPL,MSFT')

    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')

    analyzer = BoxBreakoutAnalyzer(provider=args.provider)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(',') if s.strip()]
        results = analyzer.scan_breakouts(symbols, args.period, args.days, args.volume_threshold)
    else:
        results = [analyzer.analyze(args.symbol, args.period, args.days, args.volume_threshold)]

    if args.format == 'json':
        output = _make_serializable(results if len(results) > 1 else results[0])
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(format_text(results))


if __name__ == '__main__':
    main()
