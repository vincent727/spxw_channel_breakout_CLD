"""
订单管理器模块 - 包含买方验证

SPXW 0DTE 期权自动交易系统 V4

⚠️ 核心安全约束:
1. 仅允许期权买方操作 (Long Options Only)
2. 禁止任何可能产生裸空头寸的操作
3. 卖出前必须验证持仓

SPXW Tick Size 规则:
- 价格 < $3.00: 最小变动 $0.05
- 价格 >= $3.00: 最小变动 $0.10

职责:
1. 订单创建和提交
2. 订单状态跟踪
3. 买方验证
4. 订单执行回调
5. 价格 tick size 对齐
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from ib_insync import Contract, Trade, Order, LimitOrder, MarketOrder

from core.event_bus import EventBus
from core.events import (
    OrderRequestEvent, OrderStatusEvent, FillEvent, ErrorEvent
)
from core.state import TradingState, Position, OrderRecord
from core.config import ExecutionConfig
from .price_utils import align_buy_price, align_sell_price, is_valid_price

logger = logging.getLogger(__name__)


@dataclass
class OrderRetryConfig:
    """★ 修复 7: 订单重试配置"""
    max_retries: int = 2
    retry_delay_ms: int = 200
    price_refresh_enabled: bool = True
    price_adjustment_ticks: int = 1


@dataclass
class OrderContext:
    """订单上下文"""
    order_id: int
    request_id: str
    contract: Contract
    action: str
    quantity: int
    order_type: str
    limit_price: Optional[float]
    position_id: Optional[str]
    is_stop_order: bool
    submit_time: datetime
    trade: Optional[Trade] = None
    status: str = "Pending"


class OrderManager:
    """
    订单管理器
    
    ⚠️ 核心安全原则:
    - 仅允许 Buy to Open (BTO)
    - 仅允许 Sell to Close (STC)
    - 卖出前必须验证持仓数量
    
    职责:
    1. 创建和提交订单
    2. 跟踪订单状态
    3. 处理成交回调
    4. 执行买方验证
    """
    
    def __init__(
        self,
        ib_adapter: 'IBAdapter',
        config: ExecutionConfig,
        event_bus: EventBus,
        state: TradingState
    ):
        self.ib = ib_adapter
        self.config = config
        self.event_bus = event_bus
        self.state = state
        
        # 活跃订单
        self.active_orders: Dict[int, OrderContext] = {}
        
        # ★ 修复 7: 订单重试配置和统计
        self.retry_config = OrderRetryConfig()
        self.rejected_order_history: List[Dict] = []
        self.retried_orders: int = 0
        
        # 统计
        self.total_orders: int = 0
        self.filled_orders: int = 0
        self.rejected_orders: int = 0
        
        logger.info(
            f"OrderManager initialized: "
            f"enforce_long_only={config.enforce_long_only}, "
            f"max_position={config.max_position}"
        )
    
    # ========================================================================
    # 买方验证 - CRITICAL
    # ========================================================================
    
    def _validate_buy_order(
        self,
        contract: Contract,
        quantity: int
    ) -> Tuple[bool, str]:
        """
        验证买入订单
        
        检查:
        1. 最大持仓限制
        2. 最大单笔金额
        """
        # 检查最大持仓
        current_position_count = self.state.get_total_position_count()
        if current_position_count >= self.config.max_position:
            return False, f"Max position limit reached: {current_position_count}/{self.config.max_position}"
        
        return True, ""
    
    def _validate_sell_order(
        self,
        contract: Contract,
        quantity: int
    ) -> Tuple[bool, str]:
        """
        ⚠️ CRITICAL: 验证卖出订单
        
        这是防止裸空的核心检查！
        
        必须确保:
        1. 存在该合约的持仓
        2. 持仓数量 >= 订单数量
        """
        # 查找持仓
        position = self.state.get_position_by_contract(contract.conId)
        
        if position is None:
            logger.error(
                f"BLOCKED: Sell order without position - would create naked short! "
                f"Contract: {contract.localSymbol}"
            )
            return False, "No position to sell - would create naked short"
        
        if position.quantity < quantity:
            logger.error(
                f"BLOCKED: Sell qty {quantity} > position {position.quantity} "
                f"Contract: {contract.localSymbol}"
            )
            return False, f"Sell quantity {quantity} exceeds position {position.quantity}"
        
        if position.status != "OPEN":
            return False, f"Position status is {position.status}, not OPEN"
        
        return True, ""
    
    def validate_order(
        self,
        action: str,
        contract: Contract,
        quantity: int
    ) -> Tuple[bool, str]:
        """
        验证订单
        
        Args:
            action: "BUY" or "SELL"
            contract: 期权合约
            quantity: 订单数量
        
        Returns:
            (is_valid, reason)
        """
        if not self.config.enforce_long_only:
            return True, ""
        
        if action == "BUY":
            return self._validate_buy_order(contract, quantity)
        elif action == "SELL":
            return self._validate_sell_order(contract, quantity)
        else:
            return False, f"Unknown action: {action}"
    
    # ========================================================================
    # 订单创建
    # ========================================================================
    
    async def submit_buy_order(
        self,
        contract: Contract,
        quantity: int,
        limit_price: Optional[float] = None,
        signal_id: str = ""
    ) -> Optional[OrderContext]:
        """
        提交买入订单 (Buy to Open)
        
        Args:
            contract: 期权合约
            quantity: 数量
            limit_price: 限价（None 则使用市价）
            signal_id: 关联的信号ID
        
        Returns:
            OrderContext 或 None
        """
        # 验证订单
        is_valid, reason = self.validate_order("BUY", contract, quantity)
        if not is_valid:
            logger.error(f"Buy order rejected: {reason}")
            self.rejected_orders += 1
            
            await self.event_bus.publish(ErrorEvent(
                error_type="ORDER_REJECTED",
                error_message=reason,
                component="OrderManager",
                severity="ERROR"
            ))
            return None
        
        # 对齐限价到 SPXW tick size
        aligned_price = None
        if limit_price and self.config.order_type == "LMT":
            aligned_price = align_buy_price(limit_price)
            if aligned_price != limit_price:
                logger.debug(f"Price aligned: ${limit_price:.2f} → ${aligned_price:.2f}")
        
        # 创建订单
        if aligned_price and self.config.order_type == "LMT":
            order = LimitOrder(
                action="BUY",
                totalQuantity=quantity,
                lmtPrice=aligned_price,
                tif="DAY",
                transmit=True
            )
            order_type = "LMT"
        else:
            order = MarketOrder(
                action="BUY",
                totalQuantity=quantity,
                transmit=True
            )
            order_type = "MKT"
        
        # 提交订单
        trade = await self.ib.place_order(contract, order)
        
        if not trade:
            logger.error(f"Failed to submit buy order for {contract.localSymbol}")
            return None
        
        # 创建上下文 (使用对齐后的价格)
        request_id = str(uuid.uuid4())
        final_price = aligned_price if aligned_price else limit_price
        context = OrderContext(
            order_id=trade.order.orderId,
            request_id=request_id,
            contract=contract,
            action="BUY",
            quantity=quantity,
            order_type=order_type,
            limit_price=final_price,
            position_id=None,
            is_stop_order=False,
            submit_time=datetime.now(),
            trade=trade
        )
        
        self.active_orders[trade.order.orderId] = context
        self.total_orders += 1
        
        # 注册回调 (注意: filledEvent 只传 trade，fill 从 trade.fills 获取)
        trade.statusEvent += lambda t: self._on_order_status(t, context)
        trade.filledEvent += lambda t: self._on_fill(t, context)
        # ★ 修复 7: 注册取消/拒绝回调以支持重试
        trade.cancelledEvent += lambda t: asyncio.create_task(
            self._on_order_cancelled_or_rejected(t, context)
        )
        
        # 保存订单记录
        await self.state.add_order(OrderRecord(
            order_id=trade.order.orderId,
            contract_id=contract.conId,
            contract_symbol=contract.localSymbol,
            action="BUY",
            order_type=order_type,
            quantity=quantity,
            limit_price=final_price or 0,
            status="Submitted",
            submit_time=datetime.now()
        ))
        
        logger.info(
            f"Buy order submitted: {contract.localSymbol} "
            f"Qty={quantity} @ ${final_price or 'MKT'}"
        )
        
        return context
    
    async def submit_sell_order(
        self,
        contract: Contract,
        quantity: int,
        limit_price: Optional[float] = None,
        position_id: str = "",
        is_stop_order: bool = False
    ) -> Optional[OrderContext]:
        """
        提交卖出订单 (Sell to Close)
        
        ⚠️ CRITICAL: 此方法会验证持仓，防止裸空
        
        Args:
            contract: 期权合约
            quantity: 数量
            limit_price: 限价
            position_id: 关联的持仓ID
            is_stop_order: 是否为止损订单
        
        Returns:
            OrderContext 或 None
        """
        # ⚠️ 核心验证
        is_valid, reason = self.validate_order("SELL", contract, quantity)
        if not is_valid:
            logger.error(f"SELL ORDER BLOCKED: {reason}")
            self.rejected_orders += 1
            
            await self.event_bus.publish(ErrorEvent(
                error_type="SELL_ORDER_BLOCKED",
                error_message=f"CRITICAL: {reason}",
                component="OrderManager",
                severity="CRITICAL"
            ))
            return None
        
        # 对齐限价到 SPXW tick size
        aligned_price = None
        if limit_price:
            aligned_price = align_sell_price(limit_price)
            if aligned_price != limit_price:
                logger.debug(f"Sell price aligned: ${limit_price:.2f} → ${aligned_price:.2f}")
        
        # 创建订单
        if aligned_price:
            order = LimitOrder(
                action="SELL",
                totalQuantity=quantity,
                lmtPrice=aligned_price,
                tif="GTC",  # 止损订单用 GTC
                transmit=True
            )
            order_type = "LMT"
        else:
            # 市价单（仅作为最后手段）
            order = MarketOrder(
                action="SELL",
                totalQuantity=quantity,
                transmit=True
            )
            order_type = "MKT"
            logger.warning(
                f"Using MARKET order for sell - this may result in poor fill! "
                f"Contract: {contract.localSymbol}"
            )
        
        # 提交订单
        trade = await self.ib.place_order(contract, order)
        
        if not trade:
            logger.error(f"Failed to submit sell order for {contract.localSymbol}")
            return None
        
        # 创建上下文 (使用对齐后的价格)
        request_id = str(uuid.uuid4())
        final_price = aligned_price if aligned_price else limit_price
        context = OrderContext(
            order_id=trade.order.orderId,
            request_id=request_id,
            contract=contract,
            action="SELL",
            quantity=quantity,
            order_type=order_type,
            limit_price=final_price,
            position_id=position_id,
            is_stop_order=is_stop_order,
            submit_time=datetime.now(),
            trade=trade
        )
        
        self.active_orders[trade.order.orderId] = context
        self.total_orders += 1
        
        # 注册回调 (注意: filledEvent 只传 trade，fill 从 trade.fills 获取)
        trade.statusEvent += lambda t: self._on_order_status(t, context)
        trade.filledEvent += lambda t: self._on_fill(t, context)
        
        # 保存订单记录
        await self.state.add_order(OrderRecord(
            order_id=trade.order.orderId,
            contract_id=contract.conId,
            contract_symbol=contract.localSymbol,
            action="SELL",
            order_type=order_type,
            quantity=quantity,
            limit_price=final_price or 0,
            status="Submitted",
            position_id=position_id,
            is_stop_order=is_stop_order,
            submit_time=datetime.now()
        ))
        
        logger.info(
            f"Sell order submitted: {contract.localSymbol} "
            f"Qty={quantity} @ ${final_price or 'MKT'} "
            f"{'(STOP)' if is_stop_order else ''}"
        )
        
        return context
    
    # ========================================================================
    # 订单修改
    # ========================================================================
    
    async def modify_order_price(
        self,
        order_id: int,
        new_price: float
    ) -> bool:
        """
        修改订单价格
        
        用于动态追单止损
        """
        context = self.active_orders.get(order_id)
        if not context or not context.trade:
            logger.warning(f"Order {order_id} not found or no trade object")
            return False
        
        success = await self.ib.modify_order(context.trade, new_price)
        
        if success:
            context.limit_price = new_price
            logger.debug(f"Order {order_id} price modified to ${new_price}")
        
        return success
    
    async def cancel_order(self, order_id: int) -> bool:
        """取消订单"""
        context = self.active_orders.get(order_id)
        if not context or not context.trade:
            return False
        
        success = await self.ib.cancel_order(context.trade)
        
        if success:
            context.status = "Cancelled"
            logger.info(f"Order {order_id} cancelled")
        
        return success
    
    # ========================================================================
    # 订单回调
    # ========================================================================
    
    def _on_order_status(self, trade: Trade, context: OrderContext) -> None:
        """订单状态变更回调"""
        status = trade.orderStatus.status
        context.status = status
        
        logger.debug(
            f"Order {context.order_id} status: {status} "
            f"Filled: {trade.orderStatus.filled}/{context.quantity}"
        )
        
        # 更新数据库
        asyncio.create_task(self.state.update_order_status(
            context.order_id,
            status,
            trade.orderStatus.filled,
            trade.orderStatus.avgFillPrice
        ))
        
        # 发布事件
        self.event_bus.publish_sync(OrderStatusEvent(
            order_id=context.order_id,
            status=status,
            filled=trade.orderStatus.filled,
            remaining=trade.orderStatus.remaining,
            avg_fill_price=trade.orderStatus.avgFillPrice,
            action=context.action,
            contract_symbol=context.contract.localSymbol,
            total_quantity=context.quantity
        ))
        
        # 订单完成，从活跃列表移除
        if status in ["Filled", "Cancelled", "Inactive"]:
            self.active_orders.pop(context.order_id, None)
            
            if status == "Filled":
                self.filled_orders += 1
    
    def _on_fill(self, trade: Trade, context: OrderContext) -> None:
        """
        成交回调
        
        注意: ib_insync 的 filledEvent 可能只传 trade，
        所以我们从 trade.fills 中获取最新的 fill 信息
        """
        if not trade.fills:
            logger.warning(f"Order {context.order_id} filled but no fill info")
            return
        
        # 获取最新的 fill
        fill = trade.fills[-1]
        
        logger.info(
            f"Order {context.order_id} filled: "
            f"{context.action} {fill.execution.shares} @ ${fill.execution.price}"
        )
        
        # 发布成交事件
        self.event_bus.publish_sync(FillEvent(
            order_id=context.order_id,
            contract=context.contract,
            contract_symbol=context.contract.localSymbol,
            action=context.action,
            quantity=fill.execution.shares,
            fill_price=fill.execution.price,
            commission=fill.commissionReport.commission if fill.commissionReport else 0,
            position_id=context.position_id or "",
            is_entry=(context.action == "BUY")
        ))
    
    # ========================================================================
    # 查询
    # ========================================================================
    
    def get_active_orders(self) -> List[OrderContext]:
        """获取所有活跃订单"""
        return list(self.active_orders.values())
    
    def get_order(self, order_id: int) -> Optional[OrderContext]:
        """获取订单"""
        return self.active_orders.get(order_id)
    
    def _is_retryable_rejection(self, error_message: str) -> bool:
        """
        ★ 修复 7: 判断订单拒绝是否可重试
        
        Args:
            error_message: 拒绝原因
            
        Returns:
            True if retryable
        """
        retryable_keywords = [
            "limit price",
            "aggressive",
            "market price",
            "cannot accept",
            "price is not valid",
            "price does not conform"
        ]
        
        error_lower = error_message.lower()
        return any(keyword in error_lower for keyword in retryable_keywords)
    
    async def _on_order_cancelled_or_rejected(
        self,
        trade: Trade,
        context: OrderContext
    ) -> None:
        """
        ★ 修复 7: 处理订单取消或拒绝
        
        Args:
            trade: Trade对象
            context: 订单上下文
        """
        status = trade.orderStatus.status
        
        # 记录拒绝信息
        rejection_info = {
            "order_id": context.order_id,
            "contract": context.contract.localSymbol,
            "action": context.action,
            "quantity": context.quantity,
            "limit_price": context.limit_price,
            "status": status,
            "timestamp": datetime.now()
        }
        
        # 获取拒绝原因（从 trade 的最后一个错误获取）
        # Note: ib_insync 不直接提供拒绝原因，需要从日志或其他方式获取
        # 这里我们简化处理，假设价格相关的拒绝都可重试
        
        # 只对买单进行重试（卖单通常是止损，不应重试）
        if context.action != "BUY":
            logger.debug(f"Order {context.order_id} rejected/cancelled but not a buy order, skipping retry")
            return
        
        # 检查是否已达到最大重试次数
        retry_count = getattr(context, 'retry_count', 0)
        if retry_count >= self.retry_config.max_retries:
            logger.warning(
                f"Order {context.order_id} rejected after {retry_count} retries, giving up"
            )
            self.rejected_order_history.append(rejection_info)
            return
        
        # 对于限价单，尝试重试
        if context.order_type == "LMT" and self.retry_config.price_refresh_enabled:
            logger.info(
                f"Retrying order {context.order_id} (attempt {retry_count + 1}/{self.retry_config.max_retries})"
            )
            
            # 延迟后重试
            await asyncio.sleep(self.retry_config.retry_delay_ms / 1000)
            
            # 刷新报价获取最新价格
            try:
                ticker = await self.ib.subscribe_market_data(context.contract)
                if ticker:
                    await asyncio.sleep(0.1)  # 等待报价更新
                    
                    # 获取最新买价
                    new_mid = (ticker.bid + ticker.ask) / 2 if ticker.bid and ticker.ask else None
                    
                    if new_mid:
                        # 调整价格（更激进一些）
                        from .price_utils import calculate_entry_price
                        # 增加 buffer 使其更激进
                        adjusted_price = calculate_entry_price(
                            mid=new_mid,
                            buffer=self.config.limit_price_buffer + 0.05 * (retry_count + 1)
                        )
                        
                        logger.info(
                            f"Refreshed price: old=${context.limit_price:.2f} "
                            f"new=${adjusted_price:.2f} (mid=${new_mid:.2f})"
                        )
                        
                        # 重新提交订单
                        order = LimitOrder(
                            action="BUY",
                            totalQuantity=context.quantity,
                            lmtPrice=adjusted_price,
                            tif="DAY",
                            transmit=True
                        )
                        
                        new_trade = await self.ib.place_order(context.contract, order)
                        
                        if new_trade:
                            # 更新上下文
                            context.order_id = new_trade.order.orderId
                            context.limit_price = adjusted_price
                            context.trade = new_trade
                            setattr(context, 'retry_count', retry_count + 1)
                            
                            # 更新活跃订单
                            self.active_orders[new_trade.order.orderId] = context
                            
                            # 注册回调
                            new_trade.statusEvent += lambda t: self._on_order_status(t, context)
                            new_trade.filledEvent += lambda t: self._on_fill(t, context)
                            new_trade.cancelledEvent += lambda t: asyncio.create_task(
                                self._on_order_cancelled_or_rejected(t, context)
                            )
                            
                            self.retried_orders += 1
                            
                            logger.info(f"Order retry submitted: new order_id={new_trade.order.orderId}")
                        else:
                            logger.error("Failed to submit retry order")
                            self.rejected_order_history.append(rejection_info)
                    else:
                        logger.warning("No valid price for retry")
                        self.rejected_order_history.append(rejection_info)
                else:
                    logger.warning("Failed to get ticker for retry")
                    self.rejected_order_history.append(rejection_info)
                    
            except Exception as e:
                logger.error(f"Error during order retry: {e}")
                self.rejected_order_history.append(rejection_info)
        else:
            logger.debug("Order retry not enabled or not applicable")
            self.rejected_order_history.append(rejection_info)
    
    def get_stats(self) -> Dict[str, Any]:
        """★ 修复 7: 获取统计（包含重试信息）"""
        return {
            "total_orders": self.total_orders,
            "filled_orders": self.filled_orders,
            "rejected_orders": self.rejected_orders,
            "active_orders": len(self.active_orders),
            "fill_rate": self.filled_orders / max(self.total_orders, 1),
            "retried_orders": self.retried_orders,
            "rejected_history_count": len(self.rejected_order_history)
        }
