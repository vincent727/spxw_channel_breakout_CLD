"""
止损管理器 - Tick 级监控 + 双重确认

SPXW 0DTE 期权自动交易系统 V4

特性:
1. Tick 级实时监控（不等待 K 线收盘）
2. Bad Tick 过滤 + 双重确认（防止误触发）
3. 多层止损: 硬止损、保本止损、追踪止损
4. 动态追单止损执行
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from ib_insync import Contract

from core.event_bus import EventBus, EventPriority
from core.events import (
    TickEvent, StopTriggeredEvent, StopExecutionResultEvent,
    TickSnapshotEvent, PositionUpdateEvent
)
from core.state import TradingState, Position
from core.config import RiskConfig, TickFilterConfig
from .chase_stop_executor import DynamicChaseStopExecutor, PositionStop, StopResult

logger = logging.getLogger(__name__)


@dataclass
class StopCheckResult:
    """止损检查结果"""
    triggered: bool
    stop_type: str = ""
    trigger_price: float = 0.0
    pnl_pct: float = 0.0


@dataclass
class StopConfirmState:
    """止损确认状态 - 用于双重确认"""
    first_trigger_time: float
    trigger_count: int
    stop_type: str = ""


@dataclass
class MonitoredPosition:
    """监控中的持仓"""
    position: Position
    contract: Contract
    entry_price: float
    quantity: int
    highest_price: float
    
    # 止损状态
    breakeven_active: bool = False
    breakeven_price: float = 0.0
    trailing_active: bool = False
    trailing_stop_price: float = 0.0
    
    # 最后有效 Tick
    last_valid_price: Optional[float] = None
    last_tick_time: Optional[datetime] = None


class StopManager:
    """
    止损管理器
    
    核心特性:
    1. Tick 级实时监控 - 不等待 K 线收盘
    2. Bad Tick 过滤 - 防止脏数据触发止损
    3. 双重确认 - 连续 N 个 Tick 或持续 X 毫秒
    4. 动态追单止损执行
    
    止损类型:
    - HARD_STOP: 硬止损（亏损超过阈值）
    - BREAKEVEN: 保本止损（盈利后回撤到成本）
    - TRAILING_STOP: 追踪止损（从最高点回撤）
    - TIME_STOP: 时间止损（收盘前强制平仓）
    """
    
    def __init__(
        self,
        config: RiskConfig,
        event_bus: EventBus,
        chase_executor: DynamicChaseStopExecutor,
        state: TradingState
    ):
        self.config = config
        self.event_bus = event_bus
        self.chase_executor = chase_executor
        self.state = state
        
        # 持仓监控
        self.monitored_positions: Dict[str, MonitoredPosition] = {}
        
        # 止损确认状态
        self.stop_confirmations: Dict[str, StopConfirmState] = {}
        
        # 执行中的止损（防止重复触发）
        self.executing_stops: set = set()
        
        # 订阅 Tick 事件（CRITICAL 优先级）
        self.event_bus.subscribe(
            TickEvent,
            self.on_tick,
            priority=EventPriority.CRITICAL
        )
        
        logger.info(
            f"StopManager initialized: "
            f"hard_stop={config.hard_stop_pct:.1%}, "
            f"breakeven_trigger={config.breakeven_trigger_pct:.1%}, "
            f"trailing_activation={config.trailing_activation_pct:.1%}"
        )
    
    # ========================================================================
    # 持仓监控管理
    # ========================================================================
    
    def add_position(
        self,
        position: Position,
        contract: Contract
    ) -> None:
        """添加持仓到监控"""
        monitored = MonitoredPosition(
            position=position,
            contract=contract,
            entry_price=position.entry_price,
            quantity=position.quantity,
            highest_price=position.entry_price
        )
        
        self.monitored_positions[position.id] = monitored
        
        logger.info(
            f"Position added to stop monitoring: {contract.localSymbol} "
            f"Entry=${position.entry_price:.2f}"
        )
    
    def remove_position(self, position_id: str) -> None:
        """从监控移除持仓"""
        if position_id in self.monitored_positions:
            del self.monitored_positions[position_id]
            logger.debug(f"Position removed from monitoring: {position_id}")
        
        # 清理确认状态
        if position_id in self.stop_confirmations:
            del self.stop_confirmations[position_id]
    
    def get_monitored_positions(self) -> List[MonitoredPosition]:
        """获取所有监控中的持仓"""
        return list(self.monitored_positions.values())
    
    # ========================================================================
    # Tick 处理 - 核心逻辑
    # ========================================================================
    
    async def on_tick(self, tick: TickEvent) -> None:
        """
        Tick 回调 - 含双重确认
        
        这是止损的核心入口，每个 Tick 都会触发
        """
        # 忽略无效 Tick
        if not tick.is_valid:
            return
        
        # 查找持仓
        position = self._find_position_by_contract(tick.contract_id)
        if not position:
            return
        
        # 如果正在执行止损，跳过检查（避免重复日志）
        position_id = position.position.id
        if position_id in self.executing_stops:
            return
        
        # 获取价格
        price = self._get_price_from_tick(tick)
        if price is None:
            return
        
        # 更新持仓状态
        await self._update_position_state(position, price, tick)
        
        # 检查止损条件
        stop_check = self._check_stop_conditions(position, price)
        
        if stop_check.triggered:
            # 双重确认
            if self._confirm_stop_trigger(position_id, stop_check):
                # 确认触发，执行止损
                await self._execute_stop(position, stop_check, price)
        else:
            # 重置确认状态
            self._reset_confirmation(position.position.id)
    
    def _find_position_by_contract(self, contract_id: int) -> Optional[MonitoredPosition]:
        """根据合约ID查找持仓"""
        for pos in self.monitored_positions.values():
            if pos.contract.conId == contract_id:
                return pos
        return None
    
    def _get_price_from_tick(self, tick: TickEvent) -> Optional[float]:
        """从 Tick 获取价格"""
        # 优先使用 last
        if tick.last and tick.last > 0:
            return tick.last
        
        # 其次使用 mid
        if tick.bid and tick.ask:
            return (tick.bid + tick.ask) / 2
        
        return None
    
    async def _update_position_state(
        self,
        position: MonitoredPosition,
        price: float,
        tick: TickEvent
    ) -> None:
        """更新持仓状态"""
        position.last_valid_price = price
        position.last_tick_time = tick.timestamp
        
        # 更新最高价
        if price > position.highest_price:
            position.highest_price = price
        
        # 计算盈亏
        pnl_pct = (price - position.entry_price) / position.entry_price
        
        # 检查是否激活保本止损
        if not position.breakeven_active:
            if pnl_pct >= self.config.breakeven_trigger_pct:
                position.breakeven_active = True
                position.breakeven_price = position.entry_price * 1.01  # 略高于成本
                logger.info(
                    f"Breakeven activated: {position.contract.localSymbol} "
                    f"@ ${position.breakeven_price:.2f}"
                )
        
        # 检查是否激活追踪止损
        if not position.trailing_active:
            if pnl_pct >= self.config.trailing_activation_pct:
                position.trailing_active = True
                position.trailing_stop_price = price * (1 - self.config.trailing_stop_pct)
                logger.info(
                    f"Trailing stop activated: {position.contract.localSymbol} "
                    f"@ ${position.trailing_stop_price:.2f}"
                )
        
        # 更新追踪止损价格
        if position.trailing_active:
            new_trailing_stop = price * (1 - self.config.trailing_stop_pct)
            if new_trailing_stop > position.trailing_stop_price:
                position.trailing_stop_price = new_trailing_stop
        
        # 发布位置更新事件
        self.event_bus.publish_sync(PositionUpdateEvent(
            position_id=position.position.id,
            contract_symbol=position.contract.localSymbol,
            quantity=position.quantity,
            entry_price=position.entry_price,
            current_price=price,
            unrealized_pnl=pnl_pct * position.entry_price * position.quantity * 100,
            unrealized_pnl_pct=pnl_pct,
            highest_price=position.highest_price,
            breakeven_active=position.breakeven_active,
            trailing_active=position.trailing_active
        ))
    
    # ========================================================================
    # 止损条件检查
    # ========================================================================
    
    def _check_stop_conditions(
        self,
        position: MonitoredPosition,
        price: float
    ) -> StopCheckResult:
        """检查所有止损条件"""
        pnl_pct = (price - position.entry_price) / position.entry_price
        
        # 1. 硬止损（最高优先级）
        if pnl_pct <= -self.config.hard_stop_pct:
            return StopCheckResult(
                triggered=True,
                stop_type="HARD_STOP",
                trigger_price=price,
                pnl_pct=pnl_pct
            )
        
        # 2. 追踪止损
        if position.trailing_active:
            if price <= position.trailing_stop_price:
                return StopCheckResult(
                    triggered=True,
                    stop_type="TRAILING_STOP",
                    trigger_price=price,
                    pnl_pct=pnl_pct
                )
        
        # 3. 保本止损
        if position.breakeven_active:
            if price <= position.breakeven_price:
                return StopCheckResult(
                    triggered=True,
                    stop_type="BREAKEVEN",
                    trigger_price=price,
                    pnl_pct=pnl_pct
                )
        
        return StopCheckResult(triggered=False)
    
    # ========================================================================
    # 双重确认机制
    # ========================================================================
    
    def _confirm_stop_trigger(
        self,
        position_id: str,
        stop_check: StopCheckResult
    ) -> bool:
        """
        双重确认机制 - 防止 Bad Tick 误触发
        
        需要：
        - 连续 N 个 Tick 触发，或
        - 触发条件持续 X 毫秒
        """
        now = time.perf_counter()
        filter_config = self.config.tick_filter
        
        if position_id not in self.stop_confirmations:
            self.stop_confirmations[position_id] = StopConfirmState(
                first_trigger_time=now,
                trigger_count=1,
                stop_type=stop_check.stop_type
            )
            logger.debug(
                f"Stop condition first trigger: {position_id} - {stop_check.stop_type}"
            )
            return False
        
        state = self.stop_confirmations[position_id]
        
        # 如果止损类型变了，重置
        if state.stop_type != stop_check.stop_type:
            state.first_trigger_time = now
            state.trigger_count = 1
            state.stop_type = stop_check.stop_type
            return False
        
        state.trigger_count += 1
        
        # 检查连续 Tick 数量
        if state.trigger_count >= filter_config.confirm_ticks:
            logger.info(
                f"Stop confirmed by {state.trigger_count} consecutive ticks"
            )
            del self.stop_confirmations[position_id]
            return True
        
        # 检查持续时间
        duration_ms = (now - state.first_trigger_time) * 1000
        if duration_ms >= filter_config.confirm_duration_ms:
            logger.info(
                f"Stop confirmed by {duration_ms:.0f}ms duration"
            )
            del self.stop_confirmations[position_id]
            return True
        
        return False
    
    def _reset_confirmation(self, position_id: str) -> None:
        """重置确认状态"""
        if position_id in self.stop_confirmations:
            del self.stop_confirmations[position_id]
    
    # ========================================================================
    # 止损执行
    # ========================================================================
    
    async def _execute_stop(
        self,
        position: MonitoredPosition,
        stop_check: StopCheckResult,
        trigger_price: float
    ) -> None:
        """执行止损 - 使用动态追单"""
        position_id = position.position.id
        
        # 防止重复执行
        if position_id in self.executing_stops:
            logger.debug(f"Stop already executing for {position_id}")
            return
        
        self.executing_stops.add(position_id)
        
        try:
            logger.warning(
                f"🛑 EXECUTING STOP: {position.contract.localSymbol} | "
                f"Type={stop_check.stop_type} | Price=${trigger_price:.2f} | "
                f"PnL={stop_check.pnl_pct:.1%}"
            )
            
            # 快照 Tick
            self.event_bus.publish_sync(TickSnapshotEvent(
                contract_id=position.contract.conId,
                reason=f"stop_trigger_{stop_check.stop_type}",
                price=trigger_price,
                context={
                    "entry_price": position.entry_price,
                    "highest_price": position.highest_price,
                    "pnl_pct": stop_check.pnl_pct
                }
            ))
            
            # 发布止损触发事件
            await self.event_bus.publish(StopTriggeredEvent(
                position_id=position_id,
                contract=position.contract,
                contract_symbol=position.contract.localSymbol,
                stop_type=stop_check.stop_type,
                trigger_price=trigger_price,
                entry_price=position.entry_price,
                quantity=position.quantity,
                unrealized_pnl=stop_check.pnl_pct * position.entry_price * position.quantity * 100,
                unrealized_pnl_pct=stop_check.pnl_pct
            ))
            
            # 构建止损持仓对象
            position_stop = PositionStop(
                id=position_id,
                contract=position.contract,
                contract_id=position.contract.conId,
                quantity=position.quantity,
                entry_price=position.entry_price,
                highest_price=position.highest_price,
                breakeven_active=position.breakeven_active,
                breakeven_price=position.breakeven_price,
                trailing_active=position.trailing_active
            )
            
            # 执行动态追单止损
            result = await self.chase_executor.execute_stop(
                position_stop,
                trigger_price
            )
            
            # 根据结果处理
            if result.success or result.phase == "ABANDONED":
                # 从监控列表移除
                self.remove_position(position_id)
                
                # 更新状态
                await self.state.close_position(position_id)
            
        except Exception as e:
            logger.error(f"Error executing stop: {e}", exc_info=True)
        
        finally:
            self.executing_stops.discard(position_id)
    
    # ========================================================================
    # 统计
    # ========================================================================
    
    def get_stats(self) -> Dict:
        """获取止损管理器统计"""
        return {
            "monitored_positions": len(self.monitored_positions),
            "pending_confirmations": len(self.stop_confirmations),
            "executing_stops": len(self.executing_stops),
            "positions": [
                {
                    "id": pos.position.id,
                    "symbol": pos.contract.localSymbol,
                    "entry": pos.entry_price,
                    "highest": pos.highest_price,
                    "current": pos.last_valid_price,
                    "breakeven_active": pos.breakeven_active,
                    "trailing_active": pos.trailing_active
                }
                for pos in self.monitored_positions.values()
            ]
        }
