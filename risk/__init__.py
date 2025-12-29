"""
风控层模块

SPXW 0DTE 期权自动交易系统 V4

包含:
- DynamicChaseStopExecutor: 动态追单止损执行器（V4核心）
- StopManager: 止损管理器（Tick 级监控）
- CircuitBreaker: 熔断器
"""

from .chase_stop_executor import (
    DynamicChaseStopExecutor,
    PositionStop,
    StopResult,
    StopOrderState,
)

from .stop_manager import (
    StopManager,
    MonitoredPosition,
    StopCheckResult,
    StopConfirmState,
)

from .circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerState,
)

__all__ = [
    'DynamicChaseStopExecutor',
    'PositionStop',
    'StopResult',
    'StopOrderState',
    'StopManager',
    'MonitoredPosition',
    'StopCheckResult',
    'StopConfirmState',
    'CircuitBreaker',
    'CircuitBreakerState',
]
