"""
核心模块 - 事件总线、状态管理、配置

SPXW 0DTE 期权自动交易系统 V4
"""

from .config import (
    TradingConfig,
    SystemConfig,
    IBKRConfig,
    StrategyConfig,
    RiskConfig,
    ChaseStopConfig,
    TickFilterConfig,
    ExecutionConfig,
    OptionSelectionConfig,
    NotificationConfig,
    BacktestConfig,
    load_config,
    get_config,
    set_config,
)

from .event_bus import (
    EventBus,
    EventPriority,
    Event,
    get_event_bus,
    set_event_bus,
)

from .events import (
    TickEvent,
    BarEvent,
    SPXPriceEvent,
    SignalEvent,
    OrderRequestEvent,
    OrderStatusEvent,
    FillEvent,
    StopTriggeredEvent,
    StopExecutionResultEvent,
    CircuitBreakerEvent,
    PositionUpdateEvent,
    TickSnapshotEvent,
    ConnectionEvent,
    PacingViolationEvent,
    ErrorEvent,
    SystemStatusEvent,
    TradeClosedEvent,
    DailySummaryEvent,
    NotificationEvent,
    OptionPoolRefreshEvent,
    BacktestStartEvent,
    BacktestEndEvent,
)

from .state import (
    TradingState,
    Position,
    Trade,
    OrderRecord,
    DailyStats,
    get_state,
    set_state,
)

from .calendar import (
    TradingCalendar,
    get_trading_calendar,
    get_0dte_expiry,
    is_trading_allowed,
)

from .state_logger import (
    StateLogger,
    StateChange,
    StateChangeType,
    get_state_logger,
    set_state_logger,
)

__all__ = [
    # Config
    'TradingConfig',
    'SystemConfig',
    'IBKRConfig',
    'StrategyConfig',
    'RiskConfig',
    'ChaseStopConfig',
    'TickFilterConfig',
    'ExecutionConfig',
    'OptionSelectionConfig',
    'NotificationConfig',
    'BacktestConfig',
    'load_config',
    'get_config',
    'set_config',
    
    # Event Bus
    'EventBus',
    'EventPriority',
    'Event',
    'get_event_bus',
    'set_event_bus',
    
    # Events
    'TickEvent',
    'BarEvent',
    'SPXPriceEvent',
    'SignalEvent',
    'OrderRequestEvent',
    'OrderStatusEvent',
    'FillEvent',
    'StopTriggeredEvent',
    'StopExecutionResultEvent',
    'CircuitBreakerEvent',
    'PositionUpdateEvent',
    'TickSnapshotEvent',
    'ConnectionEvent',
    'PacingViolationEvent',
    'ErrorEvent',
    'SystemStatusEvent',
    'TradeClosedEvent',
    'DailySummaryEvent',
    'NotificationEvent',
    'OptionPoolRefreshEvent',
    'BacktestStartEvent',
    'BacktestEndEvent',
    
    # State
    'TradingState',
    'Position',
    'Trade',
    'OrderRecord',
    'DailyStats',
    'get_state',
    'set_state',
    
    # Calendar
    'TradingCalendar',
    'get_trading_calendar',
    'get_0dte_expiry',
    'is_trading_allowed',
    
    # State Logger
    'StateLogger',
    'StateChange',
    'StateChangeType',
    'get_state_logger',
    'set_state_logger',
]
