"""
动态追单止损执行器 - V4 核心功能

SPXW 0DTE 期权自动交易系统 V4

⚠️ 为什么不使用市价单止损:
0DTE 期权在极端行情下，做市商会瞬间撤单导致 Bid-Ask Spread 极度扩大。
市价单会以极差的价格成交，导致灾难性滑点。

动态追单算法通过三阶段限价单实现安全止损:
Phase 1: 激进限价 - 穿透买一价争取快速成交
Phase 2: 动态追单 - 追踪市场下跌，修改订单价格
Phase 3: 紧急抛售 - 深度限价或最后手段市价单
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

from ib_insync import IB, Contract, Trade, LimitOrder, MarketOrder, Ticker

from core.event_bus import EventBus
from core.events import (
    StopExecutionResultEvent, NotificationEvent, ErrorEvent
)
from core.config import ChaseStopConfig
from core.state import Position

logger = logging.getLogger(__name__)


@dataclass
class StopResult:
    """止损执行结果"""
    success: bool
    phase: Literal["AGGRESSIVE", "CHASING", "PANIC", "DONE", "ABANDONED", "FAILED"]
    fill_price: Optional[float] = None
    pnl: Optional[float] = None
    pnl_pct: Optional[float] = None
    chase_count: int = 0
    duration_ms: float = 0.0
    slippage: Optional[float] = None


@dataclass
class StopOrderState:
    """止损订单状态"""
    order_id: int
    position: 'PositionStop'
    phase: Literal["AGGRESSIVE", "CHASING", "PANIC", "DONE", "ABANDONED", "FAILED"]
    chase_count: int = 0
    start_time: float = field(default_factory=time.perf_counter)
    fill_price: Optional[float] = None
    initial_bid: Optional[float] = None


@dataclass
class PositionStop:
    """需要止损的持仓"""
    id: str
    contract: Contract
    contract_id: int
    quantity: int
    entry_price: float
    highest_price: float
    
    # 止损状态
    breakeven_active: bool = False
    breakeven_price: float = 0.0
    trailing_active: bool = False


class DynamicChaseStopExecutor:
    """
    动态追单止损执行器
    
    核心设计原则:
    1. 永不使用市价单作为首选（避免被收割）
    2. 通过修改订单追踪市场（保留队列优先级）
    3. 多级保护，极端情况有兜底
    4. 残值过低时放弃止损（保留反转机会）
    
    算法流程:
    ┌─────────────────────────────────────────────────────────┐
    │                    STOP TRIGGERED                        │
    └─────────────────────────────────────────────────────────┘
                              │
                              ▼
    ┌─────────────────────────────────────────────────────────┐
    │  PHASE 1: AGGRESSIVE LIMIT                               │
    │  Price = Current Bid - buffer                            │
    │  Wait: 500ms                                             │
    └─────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
           Filled      Not Filled         ...
              │               │
              ▼               ▼
           ✅ DONE    ┌─────────────────────────────────────────┐
                      │  PHASE 2: DYNAMIC CHASE                  │
                      │  Loop every 500ms:                       │
                      │  - If Bid < Order Price: Modify order    │
                      │  Exit: Filled / Max chase / Timeout      │
                      └─────────────────────────────────────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
           Filled       Chase Failed      Deep Loss
              │               │               │
              ▼               ▼               ▼
           ✅ DONE    ┌─────────────────────────────────────────┐
                      │  PHASE 3: PANIC FLUSH                    │
                      │  - If Bid <= $0.20: ABANDON              │
                      │  - Else: Deep limit (Bid * 0.80)         │
                      │  - Last resort: Market order             │
                      └─────────────────────────────────────────┘
    """
    
    def __init__(
        self,
        ib_adapter: 'IBAdapter',
        config: ChaseStopConfig,
        event_bus: EventBus
    ):
        self.ib = ib_adapter
        self.config = config
        self.event_bus = event_bus
        
        logger.info(
            f"DynamicChaseStopExecutor initialized: "
            f"max_chase={config.max_chase_count}, "
            f"abandon_threshold=${config.abandon_price_threshold}"
        )
    
    async def execute_stop(
        self,
        position: PositionStop,
        trigger_price: float
    ) -> StopResult:
        """
        执行止损 - 动态追单算法入口
        
        Args:
            position: 需要止损的持仓
            trigger_price: 触发止损的价格
        
        Returns:
            StopResult 止损执行结果
        """
        start_time = time.perf_counter()
        
        logger.warning(
            f"🛑 STOP TRIGGERED: {position.contract.localSymbol} | "
            f"Entry=${position.entry_price:.2f} → Trigger=${trigger_price:.2f} | "
            f"Loss={((position.entry_price - trigger_price) / position.entry_price):.1%}"
        )
        
        # Phase 1: 激进限价
        state = await self._phase1_aggressive_limit(position)
        
        if state.phase == "DONE":
            duration = (time.perf_counter() - start_time) * 1000
            await self._report_stop_result(state, "AGGRESSIVE", duration)
            return StopResult(
                success=True,
                phase="AGGRESSIVE",
                fill_price=state.fill_price,
                chase_count=0,
                duration_ms=duration
            )
        
        # Phase 2: 动态追单
        state = await self._phase2_dynamic_chase(state)
        
        if state.phase == "DONE":
            duration = (time.perf_counter() - start_time) * 1000
            await self._report_stop_result(state, "CHASING", duration)
            return StopResult(
                success=True,
                phase="CHASING",
                fill_price=state.fill_price,
                chase_count=state.chase_count,
                duration_ms=duration
            )
        
        # Phase 3: 紧急抛售
        state = await self._phase3_panic_flush(state)
        
        duration = (time.perf_counter() - start_time) * 1000
        await self._report_stop_result(state, state.phase, duration)
        
        # 计算盈亏
        pnl = None
        pnl_pct = None
        if state.fill_price:
            pnl = (state.fill_price - position.entry_price) * position.quantity * 100
            pnl_pct = (state.fill_price - position.entry_price) / position.entry_price
        
        return StopResult(
            success=(state.phase == "DONE"),
            phase=state.phase,
            fill_price=state.fill_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            chase_count=state.chase_count,
            duration_ms=duration
        )
    
    async def _phase1_aggressive_limit(
        self,
        position: PositionStop
    ) -> StopOrderState:
        """
        阶段 1: 激进限价单
        
        策略: 以 Bid - buffer 的价格下限价单，穿透买一价争取立即成交
        """
        ticker = await self._get_fresh_quote(position.contract)
        current_bid = ticker.bid if ticker.bid else 0
        
        # 计算限价
        limit_price = round(current_bid - self.config.initial_buffer, 2)
        limit_price = max(limit_price, 0.01)
        
        logger.info(
            f"Phase 1 AGGRESSIVE: Bid=${current_bid:.2f} → Limit=${limit_price:.2f}"
        )
        
        # 创建限价单
        order = LimitOrder(
            action="SELL",
            totalQuantity=position.quantity,
            lmtPrice=limit_price,
            tif="GTC",
            transmit=True
        )
        
        trade = self.ib.ib.placeOrder(position.contract, order)
        
        state = StopOrderState(
            order_id=trade.order.orderId,
            position=position,
            phase="AGGRESSIVE",
            initial_bid=current_bid
        )
        
        # 等待初始成交检查
        await asyncio.sleep(self.config.chase_interval_ms / 1000)
        
        # 刷新订单状态
        await self.ib.sleep(0)
        
        if trade.orderStatus.status == "Filled":
            state.phase = "DONE"
            state.fill_price = trade.orderStatus.avgFillPrice
            logger.info(f"✅ Phase 1 FILLED @ ${state.fill_price:.2f}")
        else:
            state.phase = "CHASING"
        
        return state
    
    async def _phase2_dynamic_chase(
        self,
        state: StopOrderState
    ) -> StopOrderState:
        """
        阶段 2: 动态追单
        
        策略: 循环检查市场，如果 Bid 已跌破订单价格，则修改订单追踪
        """
        position = state.position
        
        while True:
            elapsed_ms = (time.perf_counter() - state.start_time) * 1000
            
            # === 退出条件检查 ===
            
            if state.chase_count >= self.config.max_chase_count:
                logger.warning(f"Phase 2 EXIT: Max chase count ({state.chase_count})")
                state.phase = "PANIC"
                break
            
            if elapsed_ms > self.config.max_chase_duration_ms:
                logger.warning(f"Phase 2 EXIT: Timeout ({elapsed_ms:.0f}ms)")
                state.phase = "PANIC"
                break
            
            # 检查当前报价
            ticker = await self._get_fresh_quote(position.contract)
            current_bid = ticker.bid if ticker.bid else 0
            
            if current_bid <= 0:
                logger.warning("Phase 2: Invalid bid, entering PANIC")
                state.phase = "PANIC"
                break
            
            # 检查当前亏损
            current_loss = (position.entry_price - current_bid) / position.entry_price
            
            if current_loss >= self.config.panic_loss_threshold:
                logger.warning(
                    f"Phase 2 EXIT: Deep loss ({current_loss:.1%} >= "
                    f"{self.config.panic_loss_threshold:.1%})"
                )
                state.phase = "PANIC"
                break
            
            # === 检查成交状态 ===
            
            trade = self._get_trade_by_id(state.order_id)
            
            if trade and trade.orderStatus.status == "Filled":
                state.phase = "DONE"
                state.fill_price = trade.orderStatus.avgFillPrice
                logger.info(f"✅ Phase 2 FILLED @ ${state.fill_price:.2f}")
                break
            
            # === 追单逻辑 ===
            
            if trade:
                current_order_price = trade.order.lmtPrice
                
                # 如果市场已跌破我们的限价，追单
                if current_bid < current_order_price - 0.01:  # 加容差避免频繁修改
                    new_price = round(current_bid - self.config.chase_buffer, 2)
                    new_price = max(new_price, 0.01)
                    
                    logger.info(
                        f"Phase 2 CHASE #{state.chase_count + 1}: "
                        f"Bid=${current_bid:.2f} < Order=${current_order_price:.2f} → "
                        f"New=${new_price:.2f}"
                    )
                    
                    # 修改订单（不是撤单重发！保留队列优先级）
                    trade.order.lmtPrice = new_price
                    self.ib.ib.placeOrder(position.contract, trade.order)
                    
                    state.chase_count += 1
            
            # 等待下一次检查
            await asyncio.sleep(self.config.chase_interval_ms / 1000)
        
        return state
    
    async def _phase3_panic_flush(
        self,
        state: StopOrderState
    ) -> StopOrderState:
        """
        阶段 3: 紧急抛售
        
        策略:
        1. 如果 Bid <= 放弃阈值: 放弃止损，保留仓位（赌反转或归零）
        2. 否则: 深度限价 (Bid * 0.80)
        3. 最后手段: 市价单
        """
        position = state.position
        ticker = await self._get_fresh_quote(position.contract)
        current_bid = ticker.bid if ticker.bid else 0
        
        # === 检查是否应放弃止损 ===
        
        if current_bid <= self.config.abandon_price_threshold:
            logger.warning(
                f"⚠️ Phase 3 ABANDON: Bid=${current_bid:.2f} <= "
                f"Threshold=${self.config.abandon_price_threshold:.2f} | "
                f"Holding position for potential reversal or expiry"
            )
            
            # 取消订单
            self._cancel_order(state.order_id)
            state.phase = "ABANDONED"
            
            # 发送通知
            await self.event_bus.publish(NotificationEvent(
                notification_type="stop_abandoned",
                title="⚠️ 止损放弃",
                message=(
                    f"合约: {position.contract.localSymbol}\n"
                    f"当前 Bid: ${current_bid:.2f}\n"
                    f"原因: 价格过低，平仓无意义\n"
                    f"操作: 保留仓位等待反转或到期归零"
                ),
                priority_level="high"
            ))
            
            return state
        
        # === Deep Marketable Limit ===
        
        panic_price = round(current_bid * self.config.panic_price_factor, 2)
        panic_price = max(panic_price, 0.01)
        
        logger.warning(
            f"🚨 Phase 3 PANIC: Bid=${current_bid:.2f} → "
            f"Panic Price=${panic_price:.2f} ({self.config.panic_price_factor:.0%})"
        )
        
        # 修改订单为 Panic 价格
        trade = self._get_trade_by_id(state.order_id)
        if trade:
            trade.order.lmtPrice = panic_price
            self.ib.ib.placeOrder(position.contract, trade.order)
        
        # 等待成交
        await asyncio.sleep(self.config.market_fallback_delay_ms / 1000)
        await self.ib.sleep(0)
        
        trade = self._get_trade_by_id(state.order_id)
        
        if trade and trade.orderStatus.status == "Filled":
            state.phase = "DONE"
            state.fill_price = trade.orderStatus.avgFillPrice
            logger.info(f"✅ Phase 3 PANIC FILLED @ ${state.fill_price:.2f}")
            return state
        
        # === 最后手段: 市价单 ===
        
        if self.config.enable_market_fallback:
            logger.error("🆘 Phase 3 LAST RESORT: Submitting MARKET order")
            
            self._cancel_order(state.order_id)
            await asyncio.sleep(0.1)
            
            mkt_order = MarketOrder(
                action="SELL",
                totalQuantity=position.quantity,
                transmit=True
            )
            
            trade = self.ib.ib.placeOrder(position.contract, mkt_order)
            state.order_id = trade.order.orderId
            
            await asyncio.sleep(0.5)
            await self.ib.sleep(0)
            
            if trade.orderStatus.status == "Filled":
                state.phase = "DONE"
                state.fill_price = trade.orderStatus.avgFillPrice
                logger.warning(f"⚠️ MARKET ORDER FILLED @ ${state.fill_price:.2f}")
            else:
                state.phase = "FAILED"
                logger.error("❌ MARKET ORDER ALSO FAILED")
        else:
            state.phase = "FAILED"
            logger.error("❌ Phase 3 FAILED: Market order disabled")
        
        return state
    
    # ========================================================================
    # 辅助方法
    # ========================================================================
    
    async def _get_fresh_quote(self, contract: Contract) -> Ticker:
        """获取最新报价"""
        self.ib.ib.reqMktData(contract, '', False, False)
        await asyncio.sleep(0.05)
        ticker = self.ib.ib.ticker(contract)
        return ticker
    
    def _get_trade_by_id(self, order_id: int) -> Optional[Trade]:
        """根据订单ID获取交易对象"""
        for trade in self.ib.ib.trades():
            if trade.order.orderId == order_id:
                return trade
        return None
    
    def _cancel_order(self, order_id: int) -> None:
        """取消订单"""
        trade = self._get_trade_by_id(order_id)
        if trade and trade.orderStatus.status not in ["Filled", "Cancelled"]:
            self.ib.ib.cancelOrder(trade.order)
    
    async def _report_stop_result(
        self,
        state: StopOrderState,
        phase: str,
        duration_ms: float
    ) -> None:
        """报告止损结果"""
        position = state.position
        
        if state.phase == "DONE" and state.fill_price:
            pnl = (state.fill_price - position.entry_price) * position.quantity * 100
            pnl_pct = (state.fill_price - position.entry_price) / position.entry_price
            
            logger.info(
                f"📊 STOP COMPLETE: {position.contract.localSymbol} | "
                f"Phase={phase} | Fill=${state.fill_price:.2f} | "
                f"PnL=${pnl:.2f} ({pnl_pct:.1%}) | "
                f"Chases={state.chase_count} | Duration={duration_ms:.0f}ms"
            )
            
            # 发布结果事件
            await self.event_bus.publish(StopExecutionResultEvent(
                position_id=position.id,
                success=True,
                phase=phase,
                fill_price=state.fill_price,
                pnl=pnl,
                pnl_pct=pnl_pct,
                chase_count=state.chase_count,
                total_duration_ms=duration_ms,
                initial_bid=state.initial_bid
            ))
        
        elif state.phase == "ABANDONED":
            logger.warning(
                f"⚠️ STOP ABANDONED: {position.contract.localSymbol} | "
                f"Position retained for expiry"
            )
            
            await self.event_bus.publish(StopExecutionResultEvent(
                position_id=position.id,
                success=False,
                phase="ABANDONED",
                chase_count=state.chase_count,
                total_duration_ms=duration_ms
            ))
        
        else:
            logger.error(
                f"❌ STOP FAILED: {position.contract.localSymbol} | "
                f"Phase={phase} | Chases={state.chase_count}"
            )
            
            await self.event_bus.publish(StopExecutionResultEvent(
                position_id=position.id,
                success=False,
                phase="FAILED",
                chase_count=state.chase_count,
                total_duration_ms=duration_ms
            ))
            
            # 发送告警
            await self.event_bus.publish(NotificationEvent(
                notification_type="stop_failed",
                title="❌ 止损失败",
                message=(
                    f"合约: {position.contract.localSymbol}\n"
                    f"入场价: ${position.entry_price:.2f}\n"
                    f"追单次数: {state.chase_count}\n"
                    f"请立即人工检查!"
                ),
                priority_level="urgent"
            ))
