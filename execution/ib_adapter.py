"""
IBKR 适配器模块 - 包含限流保护和重连机制

SPXW 0DTE 期权自动交易系统 V4

特性:
- 异步连接管理
- API 限流保护
- 自动重连
- 错误处理
- 交易日历集成
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, date
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple

from ib_insync import IB, Contract, Option, Index, Ticker, Trade, Order, LimitOrder, MarketOrder

from core.event_bus import EventBus
from core.events import (
    ConnectionEvent, PacingViolationEvent, ErrorEvent,
    TickEvent, BarEvent
)
from core.config import IBKRConfig
from core.calendar import get_trading_calendar, get_0dte_expiry

logger = logging.getLogger(__name__)

# ★ 修复 8: IBKR 错误代码分类
# 信息性消息 - 仅用于调试
IBKR_INFO_CODES = {2104, 2106, 2158, 2119}
# 警告消息 - 仅用于调试
IBKR_WARNING_CODES = {10167, 10168, 399}


class IBAdapter:
    """
    IBKR 适配器 - 含限流保护
    
    职责:
    1. 连接管理
    2. 数据订阅
    3. 订单执行
    4. 限流控制
    """
    
    def __init__(self, config: IBKRConfig, event_bus: EventBus):
        self.config = config
        self.event_bus = event_bus
        
        # 惰性加载：IB 实例在 connect() 时才创建
        # 这样可以确保它绑定到正确的事件循环
        self.ib: Optional[IB] = None
        
        # 连接状态
        self._connected: bool = False
        self._connecting: bool = False
        self._reconnect_count: int = 0
        self._shutdown: bool = False  # 关闭标志，阻止重连
        
        # 限流追踪
        self.request_timestamps: Deque[float] = deque(maxlen=100)
        self.historical_request_count: int = 0
        self.historical_request_reset_time: float = time.time()
        
        # 订阅管理
        self.active_subscriptions: Dict[int, Ticker] = {}  # contract_id -> Ticker
        self.max_subscriptions = config.rate_limit.max_subscriptions
        
        # 重试配置
        self.retry_delays = config.reconnect_delays
        
        # 回调注册
        self._tick_callbacks: List[Callable[[Ticker], None]] = []
        self._error_callbacks: List[Callable[[int, int, str, Contract], None]] = []
    
    # ========================================================================
    # 连接管理
    # ========================================================================
    
    async def connect(self) -> bool:
        """
        连接到 IBKR
        
        Returns:
            连接是否成功
        """
        if self._connected:
            return True
        
        if self._connecting:
            logger.warning("Connection already in progress")
            return False
        
        self._connecting = True
        
        try:
            # ================================================================
            # 关键修复：强制 ib_insync 使用当前运行的事件循环
            # ib_insync 内部使用 asyncio.get_event_loop() 获取循环
            # 但在 NiceGUI/uvicorn 环境中，这可能返回错误的循环
            # 我们需要确保 get_event_loop() 返回当前运行的循环
            # ================================================================
            loop = asyncio.get_running_loop()
            asyncio.set_event_loop(loop)
            
            # 惰性创建 IB 实例 - 确保在当前事件循环中创建
            if self.ib is None:
                logger.debug("Creating IB instance in current event loop")
                self.ib = IB()
            
            logger.info(
                f"Connecting to IBKR: {self.config.host}:{self.config.port} "
                f"(client_id={self.config.client_id})"
            )
            
            await self.ib.connectAsync(
                host=self.config.host,
                port=self.config.port,
                clientId=self.config.client_id,
                timeout=self.config.timeout
            )
            
            # 注册错误处理
            self.ib.errorEvent += self._on_error
            self.ib.disconnectedEvent += self._on_disconnected
            
            self._connected = True
            self._reconnect_count = 0
            
            # 发布连接事件
            await self.event_bus.publish(ConnectionEvent(
                status="CONNECTED",
                message=f"Connected to IBKR (client_id={self.config.client_id})"
            ))
            
            logger.info("Successfully connected to IBKR")
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            
            await self.event_bus.publish(ConnectionEvent(
                status="ERROR",
                message=str(e)
            ))
            
            return False
        finally:
            self._connecting = False
    
    async def disconnect(self) -> None:
        """断开连接"""
        # 设置关闭标志，阻止重连
        self._shutdown = True
        
        if self._connected and self.ib is not None:
            # 先移除事件处理器，避免触发重连
            try:
                self.ib.disconnectedEvent -= self._on_disconnected
            except Exception:
                pass
            
            self.ib.disconnect()
            self._connected = False
            
            await self.event_bus.publish(ConnectionEvent(
                status="DISCONNECTED",
                message="Disconnected from IBKR"
            ))
            
            logger.info("Disconnected from IBKR")
    
    def _on_disconnected(self) -> None:
        """断连回调"""
        self._connected = False
        logger.warning("Disconnected from IBKR")
        
        # 如果正在关闭，不触发重连
        if getattr(self, '_shutdown', False):
            return
        
        # 触发重连
        asyncio.create_task(self._reconnect())
    
    async def _reconnect(self) -> None:
        """自动重连"""
        # 如果正在关闭，不重连
        if getattr(self, '_shutdown', False):
            return
        
        if self._connecting:
            return
        
        await self.event_bus.publish(ConnectionEvent(
            status="RECONNECTING",
            message="Attempting to reconnect..."
        ))
        
        for i, delay in enumerate(self.retry_delays):
            # 检查是否应该停止重连
            if getattr(self, '_shutdown', False):
                logger.info("Reconnect cancelled due to shutdown")
                return
            
            self._reconnect_count = i + 1
            logger.info(f"Reconnect attempt {self._reconnect_count} in {delay}s...")
            
            await asyncio.sleep(delay)
            
            # 再次检查
            if getattr(self, '_shutdown', False):
                logger.info("Reconnect cancelled due to shutdown")
                return
            
            if await self.connect():
                logger.info(f"Reconnected successfully after {self._reconnect_count} attempts")
                return
        
        logger.error("All reconnect attempts failed")
        
        await self.event_bus.publish(ConnectionEvent(
            status="ERROR",
            message="All reconnect attempts failed"
        ))
    
    def _on_error(
        self,
        reqId: int,
        errorCode: int,
        errorString: str,
        contract: Contract
    ) -> None:
        """
        ★ 修复 8: 错误处理回调 - 区分信息/警告/错误
        """
        # ★ 修复 8: 信息性消息 - 使用 debug 级别
        if errorCode in IBKR_INFO_CODES:
            logger.debug(f"IBKR INFO {errorCode}: {errorString}")
            return
        
        # ★ 修复 8: 警告消息 - 使用 debug 级别
        if errorCode in IBKR_WARNING_CODES:
            logger.debug(f"IBKR WARNING {errorCode}: {errorString}")
            return
        
        # Pacing Violation 错误
        if errorCode in [162, 10090, 10091]:
            logger.warning(f"PACING VIOLATION: {errorCode} - {errorString}")
            asyncio.create_task(self._handle_pacing_violation(errorCode))
            
            # 发布事件
            self.event_bus.publish_sync(PacingViolationEvent(
                error_code=errorCode,
                error_message=errorString,
                retry_delay=self.retry_delays[0]
            ))
        
        # 连接断开
        elif errorCode == 1100:
            logger.error("Connection lost, attempting reconnect...")
            asyncio.create_task(self._reconnect())
        
        # 数据订阅问题
        elif errorCode == 10090:
            logger.warning(f"Subscription issue: {errorString}")
        
        # 订单相关错误
        elif errorCode in [201, 202, 203]:
            logger.error(f"Order error {errorCode}: {errorString}")
        
        # 一般错误
        else:
            logger.error(f"IB Error {errorCode}: {errorString}")
        
        # 调用注册的回调
        for callback in self._error_callbacks:
            try:
                callback(reqId, errorCode, errorString, contract)
            except Exception as e:
                logger.error(f"Error callback failed: {e}")
    
    async def _handle_pacing_violation(self, error_code: int) -> None:
        """处理限流违规 - 指数退避"""
        for i, delay in enumerate(self.retry_delays):
            logger.info(f"Pacing violation backoff: waiting {delay}s (attempt {i+1})")
            await asyncio.sleep(delay)
            
            # 尝试轻量请求测试恢复
            try:
                await self.ib.reqCurrentTimeAsync()
                logger.info("Connection recovered after pacing violation")
                return
            except Exception:
                continue
        
        logger.error("Failed to recover from pacing violation")
    
    @property
    def is_connected(self) -> bool:
        """检查连接状态"""
        return self._connected and self.ib.isConnected()
    
    # ========================================================================
    # 限流控制
    # ========================================================================
    
    def _check_rate_limit(self) -> bool:
        """检查是否超过速率限制"""
        now = time.time()
        
        # 清理旧记录
        while self.request_timestamps and self.request_timestamps[0] < now - 1:
            self.request_timestamps.popleft()
        
        # 检查当前速率
        max_rate = self.config.rate_limit.max_requests_per_second
        if len(self.request_timestamps) >= max_rate - 5:  # 留 5 条余量
            return False
        
        self.request_timestamps.append(now)
        return True
    
    async def _wait_for_rate_limit(self) -> None:
        """等待直到可以发送请求"""
        while not self._check_rate_limit():
            await asyncio.sleep(0.05)
    
    def _check_historical_limit(self) -> bool:
        """检查历史数据请求限制"""
        now = time.time()
        
        # 每 10 分钟重置
        if now - self.historical_request_reset_time > 600:
            self.historical_request_count = 0
            self.historical_request_reset_time = now
        
        max_requests = self.config.rate_limit.historical_requests_per_10min
        if self.historical_request_count >= max_requests - 5:
            return False
        
        self.historical_request_count += 1
        return True
    
    # ========================================================================
    # 合约解析
    # ========================================================================
    
    async def resolve_spx_contract(self) -> Optional[Contract]:
        """解析 SPX 指数合约"""
        await self._wait_for_rate_limit()
        
        contract = Index("SPX", "CBOE", "USD")
        
        try:
            contracts = await self.ib.qualifyContractsAsync(contract)
            if contracts:
                return contracts[0]
        except Exception as e:
            logger.error(f"Failed to resolve SPX contract: {e}")
        
        return None
    
    async def resolve_option_contract(
        self,
        symbol: str = "SPXW",
        strike: float = 0,
        right: str = "C",
        expiry: str = "today"
    ) -> Optional[Contract]:
        """
        解析期权合约
        
        Args:
            symbol: 期权符号 (SPXW)
            strike: 行权价
            right: "C" for Call, "P" for Put
            expiry: "today" 或具体日期 YYYYMMDD
        """
        await self._wait_for_rate_limit()
        
        # 处理到期日 - 使用交易日历
        if expiry == "today":
            expiry = get_0dte_expiry()  # 获取下一个交易日
            logger.debug(f"Using 0DTE expiry: {expiry}")
        
        # SPXW 是 SPX 的周期权
        contract = Option(
            symbol="SPX",
            lastTradeDateOrContractMonth=expiry,
            strike=strike,
            right=right,
            exchange="SMART",
            currency="USD",
            tradingClass="SPXW"  # 指定 Weekly
        )
        
        try:
            contracts = await self.ib.qualifyContractsAsync(contract)
            if contracts:
                logger.debug(f"Resolved contract: {contracts[0].localSymbol}")
                return contracts[0]
        except Exception as e:
            logger.error(f"Failed to resolve option contract: {e}")
        
        return None
    
    async def get_option_chain(
        self,
        expiry: str = "today"
    ) -> List[Contract]:
        """
        获取期权链
        
        注意: 这是一个相对慢的操作 (500ms-2s)
        """
        await self._wait_for_rate_limit()
        
        if expiry == "today":
            expiry = get_0dte_expiry()  # 获取下一个交易日
            logger.debug(f"Using 0DTE expiry for chain: {expiry}")
        
        # 获取 SPX 合约
        spx = await self.resolve_spx_contract()
        if not spx:
            return []
        
        try:
            chains = await self.ib.reqSecDefOptParamsAsync(
                spx.symbol,
                "",  # futFopExchange
                spx.secType,
                spx.conId
            )
            
            # 找到 SPXW 链
            for chain in chains:
                if chain.tradingClass == "SPXW":
                    if expiry in chain.expirations:
                        # 返回所有可用行权价
                        contracts = []
                        for strike in chain.strikes:
                            for right in ["C", "P"]:
                                contract = Option(
                                    symbol="SPX",
                                    lastTradeDateOrContractMonth=expiry,
                                    strike=strike,
                                    right=right,
                                    exchange="SMART",
                                    currency="USD",
                                    tradingClass="SPXW"
                                )
                                contracts.append(contract)
                        return contracts
            
        except Exception as e:
            logger.error(f"Failed to get option chain: {e}")
        
        return []
    
    # ========================================================================
    # 数据订阅
    # ========================================================================
    
    async def subscribe_market_data(
        self,
        contract: Contract,
        callback: Optional[Callable[[Ticker], None]] = None
    ) -> Optional[Ticker]:
        """
        订阅市场数据
        
        包含智能订阅管理
        """
        if not self.is_connected:
            logger.error("Not connected to IBKR")
            return None
        
        # 检查订阅数量限制
        if len(self.active_subscriptions) >= self.max_subscriptions:
            logger.warning("Max subscriptions reached, cleaning up...")
            await self._cleanup_stale_subscriptions()
        
        # 检查速率限制
        await self._wait_for_rate_limit()
        
        try:
            # 请求市场数据
            ticker = self.ib.reqMktData(contract, '', False, False)
            
            # 记录订阅
            self.active_subscriptions[contract.conId] = ticker
            
            # 注册回调
            if callback:
                ticker.updateEvent += callback
            
            logger.debug(f"Subscribed to market data: {contract.localSymbol}")
            return ticker
            
        except Exception as e:
            logger.error(f"Failed to subscribe market data: {e}")
            return None
    
    async def subscribe_tick_by_tick(
        self,
        contract: Contract,
        tick_type: str = "Last",
        callback: Optional[Callable[[Any], None]] = None
    ) -> bool:
        """
        订阅 Tick-by-Tick 数据
        
        tick_type: "Last", "AllLast", "BidAsk"
        """
        if not self.is_connected:
            return False
        
        await self._wait_for_rate_limit()
        
        try:
            ticker = self.ib.reqTickByTickData(
                contract,
                tick_type,
                numberOfTicks=0,
                ignoreSize=False
            )
            
            if callback:
                ticker.updateEvent += callback
            
            self.active_subscriptions[contract.conId] = ticker
            logger.debug(f"Subscribed to tick-by-tick: {contract.localSymbol}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to subscribe tick-by-tick: {e}")
            return False
    
    def unsubscribe_market_data(self, contract: Contract) -> None:
        """取消订阅"""
        if contract.conId in self.active_subscriptions:
            try:
                self.ib.cancelMktData(contract)
                del self.active_subscriptions[contract.conId]
                logger.debug(f"Unsubscribed: {contract.localSymbol}")
            except Exception as e:
                logger.error(f"Failed to unsubscribe: {e}")
    
    async def _cleanup_stale_subscriptions(self) -> None:
        """清理不再需要的订阅"""
        # 这里可以实现更复杂的逻辑
        # 例如：取消最久没有更新的订阅
        if len(self.active_subscriptions) > self.max_subscriptions * 0.9:
            # 取消一些订阅
            to_remove = list(self.active_subscriptions.keys())[:10]
            for conId in to_remove:
                ticker = self.active_subscriptions.get(conId)
                if ticker and ticker.contract:
                    self.unsubscribe_market_data(ticker.contract)
    
    def get_ticker(self, contract: Contract) -> Optional[Ticker]:
        """获取 Ticker"""
        return self.active_subscriptions.get(contract.conId)
    
    # ========================================================================
    # 历史数据
    # ========================================================================
    
    async def get_historical_bars(
        self,
        contract: Contract,
        duration: str = "1 D",
        bar_size: str = "5 mins",
        what_to_show: str = "TRADES",
        use_rth: bool = True
    ) -> List[Any]:
        """
        获取历史 K 线数据
        """
        if not self.is_connected:
            return []
        
        # 检查历史数据请求限制
        if not self._check_historical_limit():
            logger.warning("Historical data limit reached, waiting...")
            await asyncio.sleep(60)
        
        await self._wait_for_rate_limit()
        
        try:
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration,
                barSizeSetting=bar_size,
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=1
            )
            
            return bars
            
        except Exception as e:
            logger.error(f"Failed to get historical data: {e}")
            return []
    
    # ========================================================================
    # 订单执行
    # ========================================================================
    
    async def place_order(
        self,
        contract: Contract,
        order: Order
    ) -> Optional[Trade]:
        """
        下单
        
        Returns:
            Trade 对象或 None
        """
        if not self.is_connected:
            logger.error("Not connected, cannot place order")
            return None
        
        await self._wait_for_rate_limit()
        
        try:
            trade = self.ib.placeOrder(contract, order)
            logger.info(
                f"Order placed: {order.action} {order.totalQuantity} "
                f"{contract.localSymbol} @ {getattr(order, 'lmtPrice', 'MKT')}"
            )
            return trade
            
        except Exception as e:
            logger.error(f"Failed to place order: {e}")
            return None
    
    async def modify_order(
        self,
        trade: Trade,
        new_price: float
    ) -> bool:
        """
        修改订单价格
        
        注意: 修改订单比撤单重发更高效，保留队列优先级
        """
        if not self.is_connected:
            return False
        
        try:
            trade.order.lmtPrice = new_price
            self.ib.placeOrder(trade.contract, trade.order)
            logger.debug(f"Order modified: {trade.order.orderId} -> ${new_price}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to modify order: {e}")
            return False
    
    async def cancel_order(self, trade: Trade) -> bool:
        """取消订单"""
        if not self.is_connected:
            return False
        
        try:
            if trade.orderStatus.status not in ["Filled", "Cancelled"]:
                self.ib.cancelOrder(trade.order)
                logger.debug(f"Order cancelled: {trade.order.orderId}")
                return True
        except Exception as e:
            logger.error(f"Failed to cancel order: {e}")
        
        return False
    
    def get_trade_by_id(self, order_id: int) -> Optional[Trade]:
        """根据订单ID获取 Trade"""
        for trade in self.ib.trades():
            if trade.order.orderId == order_id:
                return trade
        return None
    
    # ========================================================================
    # 账户信息
    # ========================================================================
    
    async def get_account_summary(self) -> Dict[str, Any]:
        """获取账户摘要"""
        if not self.is_connected:
            return {}
        
        await self._wait_for_rate_limit()
        
        try:
            account_values = self.ib.accountValues()
            
            summary = {}
            for av in account_values:
                if av.tag in [
                    "NetLiquidation", "TotalCashValue", "BuyingPower",
                    "AvailableFunds", "ExcessLiquidity", "DayTradesRemaining"
                ]:
                    summary[av.tag] = float(av.value)
            
            return summary
            
        except Exception as e:
            logger.error(f"Failed to get account summary: {e}")
            return {}
    
    async def get_positions(self) -> List[Any]:
        """获取当前持仓"""
        if not self.is_connected:
            return []
        
        await self._wait_for_rate_limit()
        
        try:
            return self.ib.positions()
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []
    
    # ========================================================================
    # 实用方法
    # ========================================================================
    
    async def sleep(self, seconds: float) -> None:
        """
        异步等待
        
        注意：使用 asyncio.sleep 而不是 ib.sleep()，
        因为 ib.sleep() 在 asyncio.run() 中会造成事件循环冲突
        """
        await asyncio.sleep(seconds)
    
    def register_tick_callback(self, callback: Callable[[Ticker], None]) -> None:
        """注册 Tick 回调"""
        self._tick_callbacks.append(callback)
    
    def register_error_callback(
        self,
        callback: Callable[[int, int, str, Contract], None]
    ) -> None:
        """注册错误回调"""
        self._error_callbacks.append(callback)
