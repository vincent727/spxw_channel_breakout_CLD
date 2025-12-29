"""
策略层模块

SPXW 0DTE 期权自动交易系统 V4

包含:
- IndicatorEngine: 技术指标计算引擎
- ChannelBreakoutStrategy: Channel Breakout 主策略
"""

from .indicators import (
    IndicatorEngine,
    ChannelResult,
    TrendResult,
    IndicatorState,
    calculate_atr,
    calculate_rsi,
    calculate_bollinger_bands,
)

from .channel_breakout import (
    ChannelBreakoutStrategy,
    StrategyState,
)

__all__ = [
    'IndicatorEngine',
    'ChannelResult',
    'TrendResult',
    'IndicatorState',
    'calculate_atr',
    'calculate_rsi',
    'calculate_bollinger_bands',
    'ChannelBreakoutStrategy',
    'StrategyState',
]
