#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
宏观流动性监控分析器 (Macro Liquidity Analyzer)

五维度监控，每个维度有明确的触发阈值：

| 维度     | 数据源                           | 预警条件              | 权重 |
|---------|----------------------------------|----------------------|------|
| 净流动性 | FRED: WALCL - WTREGEN - RRPONTSYD | 单周下降 > 5%         | 35%  |
| SOFR    | FRED: SOFR                        | 突破 5.5%            | 20%  |
| MOVE指数 | Yahoo: ^MOVE                      | 超过 130             | 15%  |
| 日元套利 | Yahoo: JPY=X + FRED: DGS2        | USD/JPY急跌+利差收窄   | 15%  |
| 黄金异动 | Yahoo: GC=F + DX-Y.NYB, FRED: DFII10 | 连跌3天/破MA20/RSI弱势/周跌>3% | 15%  |

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
    # 黄金异动阈值
    'gold_weekly_drop_pct': 3.0,                # 周跌幅中度
    'gold_weekly_drop_severe_pct': 5.0,         # 周跌幅重度
    'gold_consecutive_down_days': 3,            # 连跌天数
    'gold_rsi_weak': 40,                        # RSI 弱势区间
    'gold_usd_divergence_gold_drop': 2.0,       # 金价周跌幅(%)
    'gold_usd_divergence_usd_max': 0.5,         # 美元周涨幅上限(%)
    'real_yield_spike_bp': 30,                  # 实际利率周升(bp)
    'real_yield_spike_moderate_bp': 15,          # 实际利率周升中度(bp)
}

# 维度权重
DIMENSION_WEIGHTS = {
    'net_liquidity': 0.35,
    'sofr': 0.20,
    'move': 0.15,
    'yen_carry': 0.15,
    'gold': 0.15,
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

    监控美联储资产负债表净流动性、SOFR、MOVE指数、日元套利交易和黄金异动，
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

        # 5. 黄金异动
        try:
            gold_result = self._fetch_gold_anomaly(lookback_days)
            dimensions['gold'] = gold_result
            dimension_scores['gold'] = gold_result.get('risk_score', 50)
            signals.extend(gold_result.get('signals', []))
        except Exception as e:
            logger.error(f"获取黄金异动数据失败: {e}")
            dimensions['gold'] = {'error': str(e)}
            dimension_scores['gold'] = 50

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


    # ==================== 黄金异动 ====================

    @staticmethod
    def _calc_rsi(close, period: int = 14):
        """Calculate RSI using Wilder smoothing."""
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta.where(delta < 0, 0.0))
        avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _calc_early_warning(self, gold_close) -> dict:
        """
        早期预警层 (最高 40 分)

        信号:
        - 连跌 >= 3 天: +15
        - 首次跌破 MA20: +15
        - RSI(14) < 40: +10
        """
        score = 0
        result = {
            'consecutive_down': False,
            'below_ma20': False,
            'rsi_weak': False,
            'triple_resonance': False,
            'consecutive_down_days': 0,
            'rsi': None,
            'score': 0,
        }

        if len(gold_close) < 22:
            return result

        # 1. 连跌天数
        daily_ret = gold_close.pct_change()
        streak = 0
        for ret in reversed(daily_ret.dropna().values):
            if ret < 0:
                streak += 1
            else:
                break
        result['consecutive_down_days'] = streak
        if streak >= THRESHOLDS['gold_consecutive_down_days']:
            result['consecutive_down'] = True
            score += 15

        # 2. MA20 交叉 (仅首次跌破)
        ma20 = gold_close.rolling(20).mean()
        if not pd.isna(ma20.iloc[-1]) and not pd.isna(ma20.iloc[-2]):
            today_below = gold_close.iloc[-1] < ma20.iloc[-1]
            yesterday_above = gold_close.iloc[-2] >= ma20.iloc[-2]
            if today_below and yesterday_above:
                result['below_ma20'] = True
                score += 15

        # 3. RSI
        rsi = self._calc_rsi(gold_close)
        current_rsi = rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else None
        result['rsi'] = round(current_rsi, 1) if current_rsi is not None else None
        if current_rsi is not None and current_rsi < THRESHOLDS['gold_rsi_weak']:
            result['rsi_weak'] = True
            score += 10

        # 三重共振
        if result['consecutive_down'] and result['below_ma20'] and result['rsi_weak']:
            result['triple_resonance'] = True

        result['score'] = min(score, 40)
        return result

    def _calc_confirmation(
        self,
        gold_weekly_change_pct: float,
        usd_weekly_change_pct,
        real_yield_weekly_change_bp,
    ) -> dict:
        """
        确认层 (最高 60 分)

        信号:
        - 周跌幅: >5% → 40, >3% → 25, >1% → 10
        - 金价-美元背离: 金跌 >2% + 美元 <+0.5% → +10
        - 实际利率飙升: >30bp → +10, >15bp → +5
        """
        score = 0
        result = {
            'weekly_drop': False,
            'gold_usd_divergence': False,
            'real_rate_spike': False,
            'score': 0,
        }

        if gold_weekly_change_pct < -THRESHOLDS['gold_weekly_drop_severe_pct']:
            result['weekly_drop'] = True
            score += 40
        elif gold_weekly_change_pct <= -THRESHOLDS['gold_weekly_drop_pct']:
            result['weekly_drop'] = True
            score += 25
        elif gold_weekly_change_pct < -1.0:
            score += 10

        if (
            usd_weekly_change_pct is not None
            and gold_weekly_change_pct < -THRESHOLDS['gold_usd_divergence_gold_drop']
            and usd_weekly_change_pct < THRESHOLDS['gold_usd_divergence_usd_max']
        ):
            result['gold_usd_divergence'] = True
            score += 10

        if real_yield_weekly_change_bp is not None:
            if real_yield_weekly_change_bp > THRESHOLDS['real_yield_spike_bp']:
                result['real_rate_spike'] = True
                score += 10
            elif real_yield_weekly_change_bp > THRESHOLDS['real_yield_spike_moderate_bp']:
                result['real_rate_spike'] = True
                score += 5

        result['score'] = min(score, 60)
        return result

    def _fetch_gold_anomaly(self, lookback_days: int = 365) -> Dict[str, Any]:
        """
        获取黄金异动数据，双层风险评分

        数据源:
        - GC=F (COMEX 黄金期货) via Yahoo Finance
        - DX-Y.NYB (美元指数) via Yahoo Finance
        - DFII10 (10Y TIPS 实际利率) via FRED
        """
        import yfinance as yf

        end = datetime.now()
        start = end - timedelta(days=lookback_days + 60)

        # === 黄金价格 (必需) ===
        gold = yf.download('GC=F', start=start, end=end, progress=False)
        if gold is None or gold.empty:
            return {'error': '黄金数据为空', 'risk_score': 50}

        if isinstance(gold.columns, pd.MultiIndex):
            gold.columns = gold.columns.get_level_values(0)

        close_col = 'Close' if 'Close' in gold.columns else 'close'
        if close_col not in gold.columns:
            return {'error': '黄金数据缺少 Close 列', 'risk_score': 50}

        gold_close = gold[close_col].dropna()
        if len(gold_close) < 25:
            return {'error': '黄金数据不足', 'risk_score': 50}

        current_price = float(gold_close.iloc[-1])
        ma20 = gold_close.rolling(20).mean()
        current_ma20 = float(ma20.iloc[-1]) if not pd.isna(ma20.iloc[-1]) else None

        # 周变化 (5 个交易日)
        gold_weekly_change_pct = (
            (gold_close.iloc[-1] - gold_close.iloc[-5]) / gold_close.iloc[-5] * 100
            if len(gold_close) >= 5 else 0
        )

        # === 美元指数 (可选) ===
        usd_weekly_change_pct = None
        usd_current = None
        try:
            usd = yf.download('DX-Y.NYB', start=start, end=end, progress=False)
            if usd is not None and not usd.empty:
                if isinstance(usd.columns, pd.MultiIndex):
                    usd.columns = usd.columns.get_level_values(0)
                usd_col = 'Close' if 'Close' in usd.columns else 'close'
                if usd_col in usd.columns:
                    usd_close = usd[usd_col].dropna()
                    if len(usd_close) >= 5:
                        usd_current = float(usd_close.iloc[-1])
                        usd_weekly_change_pct = (
                            (usd_close.iloc[-1] - usd_close.iloc[-5]) / usd_close.iloc[-5] * 100
                        )
        except Exception as e:
            logger.error(f"获取美元指数失败: {e}")

        # === 实际利率 (可选) ===
        real_yield = None
        real_yield_weekly_change_bp = None
        try:
            fred = self._get_fred()
            tips = fred.get_series('DFII10', observation_start=start)
            if tips is not None and not tips.empty:
                tips = tips.dropna()
                if len(tips) >= 5:
                    real_yield = float(tips.iloc[-1])
                    real_yield_weekly_change_bp = (tips.iloc[-1] - tips.iloc[-5]) * 100
        except Exception as e:
            logger.warning(f"获取 TIPS 实际利率失败: {e}")

        # === 评分 ===
        early_warning = self._calc_early_warning(gold_close)
        confirmation = self._calc_confirmation(
            gold_weekly_change_pct=gold_weekly_change_pct,
            usd_weekly_change_pct=usd_weekly_change_pct,
            real_yield_weekly_change_bp=real_yield_weekly_change_bp,
        )
        risk_score = min(early_warning['score'] + confirmation['score'], 100)

        # === 信号 ===
        signals = []

        if early_warning['triple_resonance']:
            signals.append(
                f'🔴 黄金三重前兆共振：连跌{early_warning["consecutive_down_days"]}天'
                f'+破MA20+RSI弱势({early_warning["rsi"]})，高度警惕'
            )
        else:
            if early_warning['consecutive_down']:
                cum_ret = (gold_close.iloc[-1] / gold_close.iloc[-early_warning['consecutive_down_days']] - 1) * 100
                signals.append(
                    f'🟠 黄金连跌{early_warning["consecutive_down_days"]}天'
                    f' (累计{cum_ret:.1f}%)，趋势转弱'
                )
            if early_warning['below_ma20']:
                signals.append(
                    f'🟠 黄金跌破20日均线 ({current_price:.0f} < MA20 {current_ma20:.0f})'
                )
            if early_warning['rsi_weak']:
                signals.append(f'🟠 黄金RSI跌入弱势区间 ({early_warning["rsi"]})')

        if confirmation['weekly_drop']:
            if gold_weekly_change_pct < -THRESHOLDS['gold_weekly_drop_severe_pct']:
                signals.append(f'🔴 黄金周暴跌 {gold_weekly_change_pct:.1f}%（阈值 -{THRESHOLDS["gold_weekly_drop_pct"]}%）')
            else:
                signals.append(f'🟠 黄金周跌 {gold_weekly_change_pct:.1f}%')

        if confirmation['gold_usd_divergence']:
            signals.append('🔴 黄金-美元背离：金跌但美元未涨，非美元驱动的抛售')

        if confirmation['real_rate_spike']:
            signals.append(f'🟠 实际利率周升{real_yield_weekly_change_bp:.0f}bp，压制黄金')

        if not signals:
            signals.append(f'🟢 黄金走势平稳 (${current_price:.0f}, 周变化{gold_weekly_change_pct:+.1f}%)')

        return {
            'current_price': round(current_price, 2),
            'weekly_change_pct': round(float(gold_weekly_change_pct), 2),
            'consecutive_down_days': early_warning['consecutive_down_days'],
            'ma20': round(current_ma20, 2) if current_ma20 else None,
            'rsi': early_warning['rsi'],
            'usd_index': round(usd_current, 2) if usd_current else None,
            'usd_weekly_change_pct': round(float(usd_weekly_change_pct), 2) if usd_weekly_change_pct is not None else None,
            'real_yield': round(real_yield, 2) if real_yield is not None else None,
            'real_yield_weekly_change': round(float(real_yield_weekly_change_bp), 1) if real_yield_weekly_change_bp is not None else None,
            'early_warning': early_warning,
            'confirmation': confirmation,
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
        'net_liquidity': '净流动性 (35%)',
        'sofr': 'SOFR     (20%)',
        'move': 'MOVE指数  (15%)',
        'yen_carry': '日元套利  (15%)',
        'gold': '黄金异动  (15%)',
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
            elif dim == 'gold':
                if 'current_price' in dim_data:
                    lines.append(f"      金价: ${dim_data['current_price']}, 周变化: {dim_data.get('weekly_change_pct', '?')}%")
                if dim_data.get('rsi') is not None:
                    lines.append(f"      RSI: {dim_data['rsi']} | 连跌: {dim_data.get('consecutive_down_days', 0)}天")

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
        description='宏观流动性监控分析器 — 五维度风险评分',
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
