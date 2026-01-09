"""
★ 修复 9: 状态机日志模块

SPXW 0DTE 期权自动交易系统 V4

提供统一的状态变更日志，用于调试和追踪系统行为。

使用方法:
    from core.state_logger import get_state_logger, StateChangeType
    
    logger = get_state_logger()
    logger.log_position_opened(position_id, contract_symbol, direction)
    logger.log_order(order_id, "SUBMITTED", contract_symbol)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class StateChangeType(Enum):
    """状态变更类型"""
    # 持仓相关
    POSITION_OPENED = "position_opened"
    POSITION_CLOSED = "position_closed"
    POSITION_DIRECTION_CHANGED = "position_direction_changed"
    
    # 止损相关
    STOP_TRIGGERED = "stop_triggered"
    STOP_EXECUTED = "stop_executed"
    
    # 信号相关
    SIGNAL_GENERATED = "signal_generated"
    SIGNAL_BLOCKED = "signal_blocked"
    
    # 订单相关
    ORDER_SUBMITTED = "order_submitted"
    ORDER_FILLED = "order_filled"
    ORDER_REJECTED = "order_rejected"
    
    # 锁相关
    LOCK_ACQUIRED = "lock_acquired"
    LOCK_RELEASED = "lock_released"
    
    # 引擎状态
    ENGINE_PHASE_CHANGED = "engine_phase_changed"
    CHANNEL_UPDATED = "channel_updated"
    TREND_UPDATED = "trend_updated"


@dataclass
class StateChange:
    """状态变更记录"""
    timestamp: datetime
    change_type: StateChangeType
    component: str
    field: str
    old_value: Any
    new_value: Any
    context: Dict[str, Any] = field(default_factory=dict)
    
    def __str__(self) -> str:
        """格式化输出"""
        return (
            f"[STATE] {self.component}.{self.field}: "
            f"{self.old_value} → {self.new_value} | "
            f"{self._format_context()}"
        )
    
    def _format_context(self) -> str:
        """格式化上下文信息"""
        if not self.context:
            return ""
        
        parts = []
        for key, value in self.context.items():
            parts.append(f"{key}={value}")
        
        return ", ".join(parts)


class StateLogger:
    """
    状态日志记录器
    
    记录系统中所有重要的状态变更，便于调试和追踪。
    """
    
    def __init__(self, max_history: int = 1000):
        """
        初始化状态日志记录器
        
        Args:
            max_history: 最大保留历史记录数
        """
        self.history: List[StateChange] = []
        self.max_history = max_history
        
        logger.info(f"StateLogger initialized: max_history={max_history}")
    
    def log(
        self,
        change_type: StateChangeType,
        component: str,
        field: str,
        old_value: Any,
        new_value: Any,
        **context
    ) -> None:
        """
        通用日志方法
        
        Args:
            change_type: 变更类型
            component: 组件名称
            field: 字段名称
            old_value: 旧值
            new_value: 新值
            **context: 额外的上下文信息
        """
        change = StateChange(
            timestamp=datetime.now(),
            change_type=change_type,
            component=component,
            field=field,
            old_value=old_value,
            new_value=new_value,
            context=context
        )
        
        self.history.append(change)
        
        # 限制历史记录大小
        if len(self.history) > self.max_history:
            self.history = self.history[-self.max_history:]
        
        # 输出到日志
        logger.info(str(change))
    
    def log_position_opened(
        self,
        position_id: str,
        contract_symbol: str,
        direction: str,
        quantity: int,
        entry_price: float
    ) -> None:
        """记录持仓开仓"""
        self.log(
            change_type=StateChangeType.POSITION_OPENED,
            component="PositionManager",
            field="position",
            old_value=None,
            new_value=position_id,
            contract=contract_symbol,
            direction=direction,
            quantity=quantity,
            entry_price=entry_price
        )
    
    def log_position_closed(
        self,
        position_id: str,
        contract_symbol: str,
        exit_price: float,
        pnl: float
    ) -> None:
        """记录持仓平仓"""
        self.log(
            change_type=StateChangeType.POSITION_CLOSED,
            component="PositionManager",
            field="position",
            old_value=position_id,
            new_value=None,
            contract=contract_symbol,
            exit_price=exit_price,
            pnl=pnl
        )
    
    def log_direction_change(
        self,
        old_direction: str,
        new_direction: str,
        reason: str
    ) -> None:
        """记录方向变更"""
        self.log(
            change_type=StateChangeType.POSITION_DIRECTION_CHANGED,
            component="TradingEngine",
            field="current_position_direction",
            old_value=old_direction,
            new_value=new_direction,
            reason=reason
        )
    
    def log_signal(
        self,
        signal_type: str,
        blocked: bool,
        reason: str = "",
        spx_price: Optional[float] = None
    ) -> None:
        """记录信号"""
        change_type = StateChangeType.SIGNAL_BLOCKED if blocked else StateChangeType.SIGNAL_GENERATED
        
        self.log(
            change_type=change_type,
            component="TradingEngine",
            field="signal",
            old_value=None,
            new_value=signal_type,
            blocked=blocked,
            reason=reason,
            spx_price=spx_price
        )
    
    def log_order(
        self,
        order_id: int,
        status: str,
        contract_symbol: str,
        action: str = "",
        quantity: int = 0,
        price: Optional[float] = None
    ) -> None:
        """记录订单状态"""
        if status == "Submitted":
            change_type = StateChangeType.ORDER_SUBMITTED
        elif status == "Filled":
            change_type = StateChangeType.ORDER_FILLED
        elif status in ["Cancelled", "Rejected"]:
            change_type = StateChangeType.ORDER_REJECTED
        else:
            change_type = StateChangeType.ORDER_SUBMITTED
        
        self.log(
            change_type=change_type,
            component="OrderManager",
            field="order_status",
            old_value=None,
            new_value=status,
            order_id=order_id,
            contract=contract_symbol,
            action=action,
            quantity=quantity,
            price=price
        )
    
    def log_stop(
        self,
        position_id: str,
        phase: str,
        triggered: bool = False,
        executed: bool = False,
        fill_price: Optional[float] = None
    ) -> None:
        """记录止损"""
        if triggered:
            change_type = StateChangeType.STOP_TRIGGERED
        elif executed:
            change_type = StateChangeType.STOP_EXECUTED
        else:
            change_type = StateChangeType.STOP_TRIGGERED
        
        self.log(
            change_type=change_type,
            component="StopManager",
            field="stop_status",
            old_value=None,
            new_value=phase,
            position_id=position_id,
            fill_price=fill_price
        )
    
    def log_lock(
        self,
        lock_name: str,
        acquired: bool,
        component: str = "System"
    ) -> None:
        """记录锁获取/释放"""
        change_type = StateChangeType.LOCK_ACQUIRED if acquired else StateChangeType.LOCK_RELEASED
        
        self.log(
            change_type=change_type,
            component=component,
            field=lock_name,
            old_value="released" if acquired else "acquired",
            new_value="acquired" if acquired else "released"
        )
    
    def log_phase_change(
        self,
        old_phase: str,
        new_phase: str,
        component: str = "TradingEngine"
    ) -> None:
        """记录引擎阶段变更"""
        self.log(
            change_type=StateChangeType.ENGINE_PHASE_CHANGED,
            component=component,
            field="phase",
            old_value=old_phase,
            new_value=new_phase
        )
    
    def get_recent_changes(self, count: int = 10) -> List[StateChange]:
        """获取最近的变更记录"""
        return self.history[-count:] if self.history else []
    
    def get_changes_by_type(self, change_type: StateChangeType) -> List[StateChange]:
        """按类型获取变更记录"""
        return [c for c in self.history if c.change_type == change_type]
    
    def get_summary(self) -> Dict[str, int]:
        """获取状态变更统计摘要"""
        summary = {}
        
        for change in self.history:
            type_name = change.change_type.value
            summary[type_name] = summary.get(type_name, 0) + 1
        
        return summary
    
    def clear_history(self) -> None:
        """清空历史记录"""
        self.history.clear()
        logger.info("State history cleared")


# 全局单例
_state_logger: Optional[StateLogger] = None


def get_state_logger() -> StateLogger:
    """获取全局状态日志记录器"""
    global _state_logger
    
    if _state_logger is None:
        _state_logger = StateLogger()
    
    return _state_logger


def set_state_logger(logger: StateLogger) -> None:
    """设置全局状态日志记录器"""
    global _state_logger
    _state_logger = logger
