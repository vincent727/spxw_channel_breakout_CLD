"""
回测模块

SPXW 0DTE 期权自动交易系统 V4
"""

from .engine import (
    BacktestEngine,
    BacktestResult,
    BacktestTrade,
    BacktestPosition,
    SimulatedBroker,
)

__all__ = [
    'BacktestEngine',
    'BacktestResult',
    'BacktestTrade',
    'BacktestPosition',
    'SimulatedBroker',
]
