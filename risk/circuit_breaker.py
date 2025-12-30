"""
熔断器模块 - 风险控制的最后防线

SPXW 0DTE 期权自动交易系统 V4

触发条件:
1. 当日亏损超限
2. 当日交易次数超限
3. 连续亏损次数
4. 手动触发
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from core.event_bus import EventBus
from core.events import CircuitBreakerEvent, NotificationEvent
from core.state import TradingState
from core.config import RiskConfig

logger = logging.getLogger(__name__)


@dataclass
class CircuitBreakerState:
    """熔断器状态"""
    is_active: bool = False
    trigger_type: str = ""
    trigger_value: float = 0.0
    trigger_time: Optional[datetime] = None
    cooldown_until: Optional[datetime] = None
    
    # 统计
    daily_pnl: float = 0.0
    daily_trades: int = 0
    consecutive_losses: int = 0


class CircuitBreaker:
    """
    熔断器 - 风险控制的最后防线
    
    功能:
    1. 监控当日盈亏
    2. 监控交易次数
    3. 监控连续亏损
    4. 触发后暂停交易
    """
    
    def __init__(
        self,
        config: RiskConfig,
        event_bus: EventBus,
        state: TradingState
    ):
        self.config = config
        self.event_bus = event_bus
        self.state_manager = state
        
        # 熔断状态
        self.circuit_state = CircuitBreakerState()
        
        # 今日日期
        self._today: str = datetime.now().strftime("%Y-%m-%d")
        
        logger.info(
            f"CircuitBreaker initialized: "
            f"daily_loss_limit=${config.daily_loss_limit}, "
            f"max_daily_trades={config.max_daily_trades}, "
            f"cooldown={config.circuit_breaker_cooldown}s"
        )
    
    def check_and_reset_day(self) -> None:
        """检查并重置每日状态"""
        today = datetime.now().strftime("%Y-%m-%d")
        
        if today != self._today:
            logger.info(f"New trading day: resetting circuit breaker state")
            self._today = today
            self.circuit_state = CircuitBreakerState()
    
    def is_trading_allowed(self) -> tuple[bool, str]:
        """
        检查是否允许交易
        
        Returns:
            (allowed, reason)
        """
        self.check_and_reset_day()
        
        # 检查熔断是否激活
        if self.circuit_state.is_active:
            # 检查冷却期是否结束
            if self.circuit_state.cooldown_until:
                if datetime.now() < self.circuit_state.cooldown_until:
                    remaining = (self.circuit_state.cooldown_until - datetime.now()).seconds
                    return False, f"Circuit breaker active, {remaining}s remaining"
                else:
                    # 冷却期结束
                    self._deactivate()
        
        return True, ""
    
    async def record_trade_result(self, pnl: float, is_win: bool) -> None:
        """
        记录交易结果
        
        Args:
            pnl: 盈亏金额
            is_win: 是否盈利
        """
        self.check_and_reset_day()
        
        # 更新统计
        self.circuit_state.daily_pnl += pnl
        self.circuit_state.daily_trades += 1
        
        if is_win:
            self.circuit_state.consecutive_losses = 0
        else:
            self.circuit_state.consecutive_losses += 1
        
        logger.debug(
            f"Trade recorded: PnL=${pnl:.2f} | "
            f"Daily=${self.circuit_state.daily_pnl:.2f} | "
            f"Trades={self.circuit_state.daily_trades} | "
            f"ConsecLosses={self.circuit_state.consecutive_losses}"
        )
        
        # 检查触发条件
        await self._check_triggers()
    
    async def _check_triggers(self) -> None:
        """检查熔断触发条件"""
        state = self.circuit_state
        
        # 1. 当日亏损超限
        if state.daily_pnl <= -self.config.daily_loss_limit:
            await self._trigger(
                "DAILY_LOSS",
                abs(state.daily_pnl),
                self.config.daily_loss_limit
            )
            return
        
        # 2. 当日交易次数超限
        if state.daily_trades >= self.config.max_daily_trades:
            await self._trigger(
                "MAX_TRADES",
                state.daily_trades,
                self.config.max_daily_trades
            )
            return
        
        # 3. 连续亏损（可配置，默认 10 次）
        max_consecutive = getattr(self.config, 'max_consecutive_losses', 10)
        if state.consecutive_losses >= max_consecutive:
            await self._trigger(
                "CONSECUTIVE_LOSSES",
                state.consecutive_losses,
                max_consecutive
            )
            return
    
    async def _trigger(
        self,
        trigger_type: str,
        trigger_value: float,
        threshold: float
    ) -> None:
        """触发熔断"""
        now = datetime.now()
        cooldown_until = now + timedelta(seconds=self.config.circuit_breaker_cooldown)
        
        self.circuit_state.is_active = True
        self.circuit_state.trigger_type = trigger_type
        self.circuit_state.trigger_value = trigger_value
        self.circuit_state.trigger_time = now
        self.circuit_state.cooldown_until = cooldown_until
        
        # 更新状态管理器
        await self.state_manager.activate_circuit_breaker(
            cooldown_until,
            f"{trigger_type}: {trigger_value} >= {threshold}"
        )
        
        logger.error(
            f"🚨 CIRCUIT BREAKER TRIGGERED: {trigger_type} | "
            f"Value={trigger_value} >= Threshold={threshold} | "
            f"Cooldown until {cooldown_until}"
        )
        
        # 发布事件
        await self.event_bus.publish(CircuitBreakerEvent(
            trigger_type=trigger_type,
            trigger_value=trigger_value,
            threshold=threshold,
            cooldown_until=cooldown_until,
            message=f"Trading halted: {trigger_type}"
        ))
        
        # 发送通知
        await self.event_bus.publish(NotificationEvent(
            notification_type="circuit_breaker",
            title="🚨 熔断触发",
            message=(
                f"触发类型: {trigger_type}\n"
                f"触发值: {trigger_value}\n"
                f"阈值: {threshold}\n"
                f"恢复时间: {cooldown_until.strftime('%H:%M:%S')}"
            ),
            priority_level="urgent"
        ))
    
    def _deactivate(self) -> None:
        """解除熔断"""
        logger.info("Circuit breaker cooldown ended, trading resumed")
        
        self.circuit_state.is_active = False
        self.circuit_state.trigger_type = ""
        self.circuit_state.cooldown_until = None
    
    async def manual_trigger(self, reason: str = "Manual trigger") -> None:
        """手动触发熔断"""
        await self._trigger("MANUAL", 0, 0)
        logger.warning(f"Circuit breaker manually triggered: {reason}")
    
    async def manual_reset(self) -> None:
        """手动重置熔断"""
        self._deactivate()
        await self.state_manager.deactivate_circuit_breaker()
        logger.info("Circuit breaker manually reset")
    
    def get_status(self) -> dict:
        """获取熔断器状态"""
        state = self.circuit_state
        
        return {
            "is_active": state.is_active,
            "trigger_type": state.trigger_type,
            "trigger_value": state.trigger_value,
            "trigger_time": state.trigger_time.isoformat() if state.trigger_time else None,
            "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
            "daily_pnl": state.daily_pnl,
            "daily_trades": state.daily_trades,
            "consecutive_losses": state.consecutive_losses,
            "daily_loss_limit": self.config.daily_loss_limit,
            "max_daily_trades": self.config.max_daily_trades
        }
