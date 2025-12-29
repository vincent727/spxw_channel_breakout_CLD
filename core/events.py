"""
事件定义模块 - 系统中所有事件类型

SPXW 0DTE 期权自动交易系统 V4

事件分类:
- 市场数据事件: TickEvent, BarEvent
- 交易事件: SignalEvent, OrderEvent, FillEvent
- 风控事件: StopTriggeredEvent, CircuitBreakerEvent
- 系统事件: ConnectionEvent, ErrorEvent
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from .event_bus import Event, EventPriority


# ============================================================================
# 市场数据事件
# ============================================================================

@dataclass
class TickEvent(Event):
    """
    Tick 数据事件
    
    由 TickStreamer 发布，StopManager 订阅（CRITICAL优先级）
    """
    symbol: str = ""
    contract_id: int = 0
    last: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[int] = None
    ask_size: Optional[int] = None
    volume: Optional[int] = None
    
    # Tick 验证标记（由 TickStreamer 设置）
    is_valid: bool = True
    validation_reason: str = ""
    
    priority: EventPriority = EventPriority.CRITICAL
    
    @property
    def mid(self) -> Optional[float]:
        """计算中间价"""
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return None
    
    @property
    def spread(self) -> Optional[float]:
        """计算买卖价差"""
        if self.bid is not None and self.ask is not None:
            return self.ask - self.bid
        return None


@dataclass
class BarEvent(Event):
    """
    K线数据事件
    
    由 DataManager 发布，Strategy 订阅
    """
    symbol: str = ""
    timeframe: str = "5 mins"
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    bar_time: datetime = field(default_factory=datetime.now)
    is_complete: bool = True
    
    priority: EventPriority = EventPriority.NORMAL
    
    @property
    def body_high(self) -> float:
        """K线实体高点"""
        return max(self.open, self.close)
    
    @property
    def body_low(self) -> float:
        """K线实体低点"""
        return min(self.open, self.close)


@dataclass
class SPXPriceEvent(Event):
    """SPX 价格更新事件"""
    price: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    
    priority: EventPriority = EventPriority.HIGH


# ============================================================================
# 交易信号事件
# ============================================================================

@dataclass
class SignalEvent(Event):
    """
    交易信号事件
    
    由 Strategy 发布，OrderManager 订阅
    """
    signal_type: Literal["LONG_CALL", "LONG_PUT", "CLOSE"] = "LONG_CALL"
    direction: Literal["BUY", "SELL"] = "BUY"
    strength: float = 1.0  # 信号强度 0-1
    reason: str = ""
    
    # 策略上下文
    channel_upper: Optional[float] = None
    channel_lower: Optional[float] = None
    spx_price: Optional[float] = None
    regime: Optional[str] = None
    
    priority: EventPriority = EventPriority.HIGH


@dataclass 
class OrderRequestEvent(Event):
    """
    订单请求事件
    
    由 Strategy 发布，OrderManager 订阅
    """
    action: Literal["BUY", "SELL"] = "BUY"
    contract: Any = None  # IB Contract
    quantity: int = 1
    order_type: Literal["LMT", "MKT"] = "LMT"
    limit_price: Optional[float] = None
    
    # 关联信号
    signal_id: str = ""
    
    priority: EventPriority = EventPriority.HIGH


@dataclass
class OrderStatusEvent(Event):
    """
    订单状态变更事件
    
    由 OrderManager 发布
    """
    order_id: int = 0
    status: Literal[
        "PendingSubmit", "Submitted", "PreSubmitted",
        "Filled", "Cancelled", "Inactive", "ApiCancelled"
    ] = "Submitted"
    filled: int = 0
    remaining: int = 0
    avg_fill_price: float = 0.0
    last_fill_price: float = 0.0
    
    # 订单详情
    action: str = ""
    contract_symbol: str = ""
    total_quantity: int = 0
    
    priority: EventPriority = EventPriority.HIGH


@dataclass
class FillEvent(Event):
    """
    成交事件
    
    由 OrderManager 发布，PerformanceTracker/StopManager 订阅
    """
    order_id: int = 0
    contract: Any = None
    contract_symbol: str = ""
    action: Literal["BUY", "SELL"] = "BUY"
    quantity: int = 1
    fill_price: float = 0.0
    commission: float = 0.0
    
    # 仓位信息
    position_id: str = ""
    is_entry: bool = True  # True=开仓, False=平仓
    
    priority: EventPriority = EventPriority.HIGH


# ============================================================================
# 风控事件
# ============================================================================

@dataclass
class StopTriggeredEvent(Event):
    """
    止损触发事件
    
    由 StopManager 发布
    """
    position_id: str = ""
    contract: Any = None
    contract_symbol: str = ""
    stop_type: Literal["HARD_STOP", "TRAILING_STOP", "BREAKEVEN", "TIME_STOP"] = "HARD_STOP"
    trigger_price: float = 0.0
    entry_price: float = 0.0
    quantity: int = 1
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    
    priority: EventPriority = EventPriority.CRITICAL


@dataclass
class StopExecutionResultEvent(Event):
    """
    止损执行结果事件
    
    由 ChaseStopExecutor 发布
    """
    position_id: str = ""
    success: bool = False
    phase: Literal["AGGRESSIVE", "CHASING", "PANIC", "DONE", "ABANDONED", "FAILED"] = "DONE"
    fill_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    chase_count: int = 0
    total_duration_ms: float = 0.0
    
    # 执行详情
    initial_bid: Optional[float] = None
    final_bid: Optional[float] = None
    slippage: Optional[float] = None
    
    priority: EventPriority = EventPriority.HIGH


@dataclass
class CircuitBreakerEvent(Event):
    """
    熔断器事件
    
    由 CircuitBreaker 发布
    """
    trigger_type: Literal["DAILY_LOSS", "MAX_TRADES", "CONSECUTIVE_LOSSES", "MANUAL"] = "DAILY_LOSS"
    trigger_value: float = 0.0
    threshold: float = 0.0
    cooldown_until: Optional[datetime] = None
    message: str = ""
    
    priority: EventPriority = EventPriority.CRITICAL


@dataclass
class PositionUpdateEvent(Event):
    """仓位更新事件"""
    position_id: str = ""
    contract_symbol: str = ""
    quantity: int = 0
    entry_price: float = 0.0
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    highest_price: float = 0.0
    
    # 止损状态
    breakeven_active: bool = False
    trailing_active: bool = False
    
    priority: EventPriority = EventPriority.NORMAL


# ============================================================================
# Tick 快照事件
# ============================================================================

@dataclass
class TickSnapshotEvent(Event):
    """
    Tick 快照事件 - 用于记录关键时刻的 Tick 数据
    
    场景: 止损触发、异常价格等
    """
    contract_id: int = 0
    reason: str = ""
    price: float = 0.0
    bid: Optional[float] = None
    ask: Optional[float] = None
    
    # 上下文
    context: Dict[str, Any] = field(default_factory=dict)
    
    priority: EventPriority = EventPriority.LOW


# ============================================================================
# 系统事件
# ============================================================================

@dataclass
class ConnectionEvent(Event):
    """
    连接状态事件
    
    由 IBAdapter 发布
    """
    status: Literal["CONNECTED", "DISCONNECTED", "RECONNECTING", "ERROR"] = "CONNECTED"
    message: str = ""
    error_code: Optional[int] = None
    
    priority: EventPriority = EventPriority.HIGH


@dataclass
class PacingViolationEvent(Event):
    """
    API 限流违规事件
    
    由 IBAdapter 发布
    """
    error_code: int = 0
    error_message: str = ""
    retry_delay: int = 0
    retry_attempt: int = 0
    
    priority: EventPriority = EventPriority.HIGH


@dataclass
class ErrorEvent(Event):
    """
    系统错误事件
    """
    error_type: str = ""
    error_message: str = ""
    error_code: Optional[int] = None
    component: str = ""
    stack_trace: Optional[str] = None
    
    # 严重程度
    severity: Literal["WARNING", "ERROR", "CRITICAL"] = "ERROR"
    
    priority: EventPriority = EventPriority.HIGH


@dataclass
class SystemStatusEvent(Event):
    """系统状态事件"""
    status: Literal["STARTING", "RUNNING", "PAUSED", "STOPPING", "STOPPED"] = "RUNNING"
    message: str = ""
    
    priority: EventPriority = EventPriority.NORMAL


# ============================================================================
# 绩效事件
# ============================================================================

@dataclass
class TradeClosedEvent(Event):
    """
    交易关闭事件
    
    由 PerformanceTracker 发布
    """
    trade_id: str = ""
    position_id: str = ""
    contract_symbol: str = ""
    direction: Literal["LONG_CALL", "LONG_PUT"] = "LONG_CALL"
    entry_time: datetime = field(default_factory=datetime.now)
    exit_time: datetime = field(default_factory=datetime.now)
    entry_price: float = 0.0
    exit_price: float = 0.0
    quantity: int = 1
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    commission: float = 0.0
    holding_time_seconds: int = 0
    exit_reason: str = ""
    
    priority: EventPriority = EventPriority.NORMAL


@dataclass
class DailySummaryEvent(Event):
    """每日汇总事件"""
    date: str = ""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: Optional[float] = None
    
    priority: EventPriority = EventPriority.LOW


# ============================================================================
# 通知事件
# ============================================================================

@dataclass
class NotificationEvent(Event):
    """
    通知事件
    
    由各组件发布，Notifier 订阅
    """
    notification_type: str = ""
    title: str = ""
    message: str = ""
    priority_level: Literal["low", "normal", "high", "urgent"] = "normal"
    
    # 通知渠道
    channels: List[str] = field(default_factory=lambda: ["telegram"])
    
    # 附加数据
    data: Dict[str, Any] = field(default_factory=dict)
    
    priority: EventPriority = EventPriority.LOW


# ============================================================================
# 期权池事件
# ============================================================================

@dataclass
class OptionPoolRefreshEvent(Event):
    """期权池刷新事件"""
    atm_strike: float = 0.0
    contracts_loaded: int = 0
    refresh_reason: str = ""  # "price_change", "time_interval", "initial"
    
    priority: EventPriority = EventPriority.NORMAL


# ============================================================================
# 回测事件
# ============================================================================

@dataclass
class BacktestStartEvent(Event):
    """回测开始事件"""
    start_date: str = ""
    end_date: str = ""
    initial_capital: float = 0.0
    
    priority: EventPriority = EventPriority.NORMAL


@dataclass
class BacktestEndEvent(Event):
    """回测结束事件"""
    total_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    
    priority: EventPriority = EventPriority.NORMAL
