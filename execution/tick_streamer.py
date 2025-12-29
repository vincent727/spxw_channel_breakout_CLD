"""
Tick 数据流管理器 - 包含 Bad Tick 过滤

SPXW 0DTE 期权自动交易系统 V4

特性:
1. Tick 数据实时订阅
2. Bad Tick 过滤（Mid 偏离、价格跳变）
3. 断流检测
4. 事件发布
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Set

from ib_insync import Contract, Ticker

from core.event_bus import EventBus, EventPriority
from core.events import TickEvent, ErrorEvent
from core.config import TickConfig, TickFilterConfig

logger = logging.getLogger(__name__)


@dataclass
class TickValidationResult:
    """Tick 验证结果"""
    is_valid: bool
    reason: str = ""


@dataclass
class StreamState:
    """流状态"""
    contract_id: int
    last_tick_time: float = field(default_factory=time.time)
    last_valid_tick: Optional[TickEvent] = None
    tick_count: int = 0
    invalid_tick_count: int = 0
    is_stale: bool = False


class TickStreamer:
    """
    Tick 数据流管理器 - 含 Bad Tick 过滤
    
    职责:
    1. 管理 Tick 数据订阅
    2. 过滤无效 Tick
    3. 检测数据断流
    4. 发布 Tick 事件
    """
    
    def __init__(
        self,
        ib_adapter: 'IBAdapter',
        config: TickConfig,
        filter_config: TickFilterConfig,
        event_bus: EventBus
    ):
        self.ib = ib_adapter
        self.config = config
        self.filter_config = filter_config
        self.event_bus = event_bus
        
        # 订阅管理
        self.subscriptions: Dict[int, Ticker] = {}  # contract_id -> Ticker
        self.stream_states: Dict[int, StreamState] = {}
        
        # 断流检测任务
        self._stale_check_task: Optional[asyncio.Task] = None
        self._running: bool = False
        
        logger.info(
            f"TickStreamer initialized: filter_enabled={filter_config.enabled}, "
            f"stale_threshold={config.stale_threshold_seconds}s"
        )
    
    async def start(self) -> None:
        """启动断流检测"""
        self._running = True
        self._stale_check_task = asyncio.create_task(self._check_stale_streams())
        logger.info("TickStreamer started")
    
    async def stop(self) -> None:
        """停止"""
        if not self._running:
            return
        self._running = False
        if self._stale_check_task:
            self._stale_check_task.cancel()
            try:
                await self._stale_check_task
            except asyncio.CancelledError:
                pass
        logger.info("TickStreamer stopped")
    
    # ========================================================================
    # 订阅管理
    # ========================================================================
    
    async def subscribe(self, contract: Contract) -> bool:
        """
        订阅 Tick 数据
        
        Returns:
            订阅是否成功
        """
        contract_id = contract.conId
        
        if contract_id in self.subscriptions:
            logger.debug(f"Already subscribed: {contract.localSymbol}")
            return True
        
        try:
            # 订阅市场数据
            ticker = await self.ib.subscribe_market_data(
                contract,
                callback=lambda t: self._on_ticker_update(t)
            )
            
            if ticker:
                self.subscriptions[contract_id] = ticker
                self.stream_states[contract_id] = StreamState(contract_id=contract_id)
                logger.info(f"Subscribed to tick stream: {contract.localSymbol}")
                return True
            
        except Exception as e:
            logger.error(f"Failed to subscribe tick stream: {e}")
        
        return False
    
    async def subscribe_tick_by_tick(self, contract: Contract) -> bool:
        """
        订阅 Tick-by-Tick 数据（更高精度）
        """
        contract_id = contract.conId
        
        if contract_id in self.subscriptions:
            return True
        
        try:
            success = await self.ib.subscribe_tick_by_tick(
                contract,
                tick_type=self.config.tick_type,
                callback=lambda t: self._on_tick_by_tick(t, contract)
            )
            
            if success:
                self.stream_states[contract_id] = StreamState(contract_id=contract_id)
                logger.info(f"Subscribed to tick-by-tick: {contract.localSymbol}")
                return True
            
        except Exception as e:
            logger.error(f"Failed to subscribe tick-by-tick: {e}")
        
        return False
    
    def unsubscribe(self, contract: Contract) -> None:
        """取消订阅"""
        contract_id = contract.conId
        
        if contract_id in self.subscriptions:
            self.ib.unsubscribe_market_data(contract)
            del self.subscriptions[contract_id]
        
        if contract_id in self.stream_states:
            del self.stream_states[contract_id]
        
        logger.debug(f"Unsubscribed: {contract.localSymbol}")
    
    def unsubscribe_all(self) -> None:
        """取消所有订阅"""
        for contract_id, ticker in list(self.subscriptions.items()):
            if ticker.contract:
                self.ib.unsubscribe_market_data(ticker.contract)
        
        self.subscriptions.clear()
        self.stream_states.clear()
        logger.info("All tick subscriptions cancelled")
    
    # ========================================================================
    # Tick 回调处理
    # ========================================================================
    
    def _on_ticker_update(self, ticker: Ticker) -> None:
        """Ticker 更新回调"""
        if not ticker.contract:
            return
        
        contract_id = ticker.contract.conId
        now = datetime.now()
        
        # 构建 Tick 事件
        tick = TickEvent(
            symbol=ticker.contract.localSymbol,
            contract_id=contract_id,
            last=ticker.last,
            bid=ticker.bid,
            ask=ticker.ask,
            bid_size=ticker.bidSize,
            ask_size=ticker.askSize,
            volume=ticker.volume,
            timestamp=now
        )
        
        # 验证 Tick
        state = self.stream_states.get(contract_id)
        if state:
            validation = self._validate_tick(tick, state.last_valid_tick)
            tick.is_valid = validation.is_valid
            tick.validation_reason = validation.reason
            
            # 更新状态
            state.last_tick_time = time.time()
            state.tick_count += 1
            state.is_stale = False
            
            if validation.is_valid:
                state.last_valid_tick = tick
            else:
                state.invalid_tick_count += 1
                logger.debug(
                    f"Filtered tick for {tick.symbol}: {validation.reason}"
                )
        
        # 发布事件（包含验证信息，让订阅者决定是否使用）
        self.event_bus.publish_sync(tick)
    
    def _on_tick_by_tick(self, tick_data: Any, contract: Contract) -> None:
        """Tick-by-Tick 回调"""
        contract_id = contract.conId
        now = datetime.now()
        
        # 根据 tick_data 类型构建事件
        tick = TickEvent(
            symbol=contract.localSymbol,
            contract_id=contract_id,
            last=getattr(tick_data, 'price', None),
            bid=None,
            ask=None,
            volume=getattr(tick_data, 'size', None),
            timestamp=now
        )
        
        # 验证
        state = self.stream_states.get(contract_id)
        if state:
            validation = self._validate_tick(tick, state.last_valid_tick)
            tick.is_valid = validation.is_valid
            tick.validation_reason = validation.reason
            
            state.last_tick_time = time.time()
            state.tick_count += 1
            
            if validation.is_valid:
                state.last_valid_tick = tick
            else:
                state.invalid_tick_count += 1
        
        self.event_bus.publish_sync(tick)
    
    # ========================================================================
    # Tick 验证
    # ========================================================================
    
    def _validate_tick(
        self,
        tick: TickEvent,
        prev_tick: Optional[TickEvent]
    ) -> TickValidationResult:
        """
        验证 Tick 数据质量
        
        过滤条件:
        1. 基本有效性检查
        2. Mid-price 偏离检查
        3. 价格跳变检查
        """
        if not self.filter_config.enabled:
            return TickValidationResult(is_valid=True)
        
        # 1. 基本有效性
        if tick.last is None or tick.last <= 0:
            if tick.bid is None or tick.ask is None:
                return TickValidationResult(
                    is_valid=False,
                    reason="No valid price data"
                )
        
        # 2. Mid-price 偏离检查
        if tick.bid and tick.ask and tick.last:
            mid = (tick.bid + tick.ask) / 2
            if mid > 0:
                deviation = abs(tick.last - mid) / mid
                if deviation > self.filter_config.mid_deviation_threshold:
                    return TickValidationResult(
                        is_valid=False,
                        reason=f"Mid deviation {deviation:.1%} > threshold"
                    )
        
        # 3. 价格跳变检查
        if prev_tick and prev_tick.last and tick.last:
            jump = abs(tick.last - prev_tick.last) / prev_tick.last
            if jump > self.filter_config.jump_threshold:
                return TickValidationResult(
                    is_valid=False,
                    reason=f"Price jump {jump:.1%} > threshold"
                )
        
        return TickValidationResult(is_valid=True)
    
    # ========================================================================
    # 断流检测
    # ========================================================================
    
    async def _check_stale_streams(self) -> None:
        """检测断流的数据流"""
        while self._running:
            try:
                now = time.time()
                threshold = self.config.stale_threshold_seconds
                
                for contract_id, state in self.stream_states.items():
                    elapsed = now - state.last_tick_time
                    
                    if elapsed > threshold and not state.is_stale:
                        state.is_stale = True
                        
                        # 获取合约符号
                        ticker = self.subscriptions.get(contract_id)
                        symbol = ticker.contract.localSymbol if ticker and ticker.contract else str(contract_id)
                        
                        logger.warning(
                            f"Tick stream stale: {symbol} - "
                            f"no data for {elapsed:.1f}s"
                        )
                        
                        # 发布错误事件
                        await self.event_bus.publish(ErrorEvent(
                            error_type="TICK_STREAM_STALE",
                            error_message=f"No tick data for {elapsed:.1f}s",
                            component="TickStreamer",
                            severity="WARNING"
                        ))
                        
                        # 根据配置决定操作
                        if self.config.stale_action == "close_position":
                            # 触发紧急平仓（由其他组件处理）
                            pass
                
                await asyncio.sleep(1)
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in stale check: {e}")
                await asyncio.sleep(1)
    
    def is_stream_stale(self, contract_id: int) -> bool:
        """检查流是否断流"""
        state = self.stream_states.get(contract_id)
        if not state:
            return True
        
        elapsed = time.time() - state.last_tick_time
        return elapsed > self.config.stale_threshold_seconds
    
    # ========================================================================
    # 统计
    # ========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        stats = {}
        
        for contract_id, state in self.stream_states.items():
            ticker = self.subscriptions.get(contract_id)
            symbol = ticker.contract.localSymbol if ticker and ticker.contract else str(contract_id)
            
            stats[symbol] = {
                "tick_count": state.tick_count,
                "invalid_tick_count": state.invalid_tick_count,
                "invalid_rate": state.invalid_tick_count / max(state.tick_count, 1),
                "is_stale": state.is_stale,
                "seconds_since_last_tick": time.time() - state.last_tick_time
            }
        
        return {
            "subscriptions": len(self.subscriptions),
            "streams": stats
        }
    
    def get_last_valid_tick(self, contract_id: int) -> Optional[TickEvent]:
        """获取最后一个有效 Tick"""
        state = self.stream_states.get(contract_id)
        return state.last_valid_tick if state else None
