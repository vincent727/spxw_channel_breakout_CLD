"""
执行层模块

SPXW 0DTE 期权自动交易系统 V4

包含:
- IBAdapter: IBKR API 适配器
- OptionPool: 期权预加载池
- TickStreamer: Tick 数据流管理
- OptionSelector: 期权选择器
- OrderManager: 订单管理
"""

from .ib_adapter import IBAdapter
from .option_pool import OptionPool, CachedContract
from .tick_streamer import TickStreamer, TickValidationResult
from .option_selector import OptionSelector, OptionCandidate, LiquidityCheck
from .order_manager import OrderManager, OrderContext

__all__ = [
    'IBAdapter',
    'OptionPool',
    'CachedContract',
    'TickStreamer',
    'TickValidationResult',
    'OptionSelector',
    'OptionCandidate',
    'LiquidityCheck',
    'OrderManager',
    'OrderContext',
]
