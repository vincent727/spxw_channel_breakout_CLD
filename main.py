#!/usr/bin/env python3
"""
SPXW 0DTE 期权自动交易系统 - 主程序入口

V4 特性:
- 动态追单止损（非市价单）
- Tick 级止损监控 + Bad Tick 过滤
- 期权预加载池
- 事件驱动架构
- 5 秒 K 线聚合
- 按日期分割日志

使用方法:
    python main.py                    # 使用默认配置
    python main.py --config path.yaml # 使用指定配置
    python main.py --mode paper       # 模拟交易模式
"""

from __future__ import annotations

# ============================================================================
# 关键：在任何 asyncio 操作之前设置事件循环策略
# Windows 默认使用 ProactorEventLoop，与 ib_insync 不兼容
# 必须使用 SelectorEventLoop
# ============================================================================
import sys
if sys.platform == 'win32':
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import argparse
import asyncio
import logging
import math
import signal
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

# 配置日志（在其他模块导入之前）
from core.logging_config import setup_logging
setup_logging(log_dir="logs", log_level=logging.INFO)
logger = logging.getLogger(__name__)

from core import (
    TradingConfig, load_config, set_config,
    EventBus, set_event_bus,
    TradingState, set_state,
    ConnectionEvent, SystemStatusEvent, SignalEvent, FillEvent, StopExecutionResultEvent
)
from core.calendar import get_trading_calendar, get_0dte_expiry
from execution import (
    IBAdapter, OptionPool, TickStreamer, OptionSelector, OrderManager
)
from strategy import ChannelBreakoutStrategy
from risk import DynamicChaseStopExecutor, StopManager, CircuitBreaker
from analytics.data_manager import DataManager
from analytics.bar_aggregator import BarAggregator
from core.trading_engine import TradingEngine


class TradingSystem:
    """
    交易系统主类
    
    实现工作流:
    Phase 1: Warm-up (预热) - 下载历史数据，预计算通道/趋势
    Phase 2: Subscribe Early (提前订阅) - 订阅实时数据
    Phase 3: Immediate Action (即时行动) - 开盘后立即可交易
    Phase 4: Dynamic Update (动态更新) - K线完成时更新状态
    
    关键原则:
    - ZERO HARDCODING: 所有参数来自配置
    - 配置驱动设计: 只需修改 YAML 即可改变行为
    """
    
    def __init__(self, config: TradingConfig):
        self.config = config
        
        # 核心组件
        self.event_bus: Optional[EventBus] = None
        self.state: Optional[TradingState] = None
        self.data_manager: Optional[DataManager] = None
        
        # 交易引擎 (实现 Warm-up 工作流)
        self.trading_engine: Optional['TradingEngine'] = None
        
        # 执行层组件
        self.ib_adapter: Optional[IBAdapter] = None
        self.option_pool: Optional[OptionPool] = None
        self.tick_streamer: Optional[TickStreamer] = None
        self.option_selector: Optional[OptionSelector] = None
        self.order_manager: Optional[OrderManager] = None
        
        # K 线聚合器 (备用，engine 内置聚合)
        self.bar_aggregator: Optional[BarAggregator] = None
        
        # 策略层组件 (可选，engine 已内置策略逻辑)
        self.strategy: Optional[ChannelBreakoutStrategy] = None
        
        # 风控层组件
        self.chase_executor: Optional[DynamicChaseStopExecutor] = None
        self.stop_manager: Optional[StopManager] = None
        self.circuit_breaker: Optional[CircuitBreaker] = None
        
        # SPX 合约（用于 K 线订阅）
        self._spx_contract = None
        
        # ★ 持仓合约映射（用于止损后取消 tick 订阅）
        # position_id -> Contract
        self._position_contracts: dict = {}
        
        # ★★★ 修复 1: 添加全局开仓锁 + 单一状态源 ★★★
        # 防止并发信号竞态条件导致重复开仓
        self._entry_lock: Optional[asyncio.Lock] = None  # 开仓流程保护锁
        self._current_position_id: Optional[str] = None  # 当前持仓ID (单一状态源)
        self._current_position_direction: str = ""  # 当前持仓方向 "CALL" 或 "PUT" (单一状态源)
        self._current_position_contract: Optional[Contract] = None  # 当前持仓合约
        
        # 运行状态
        self._running: bool = False
        self._stopped: bool = False  # 防止重复调用 stop()
        self._eod_close_triggered: bool = False  # EOD 平仓是否已触发
        self._stop_lock: Optional[asyncio.Lock] = None  # 保护 stop() 方法
        # 惰性初始化：在 initialize() 中创建，确保绑定到正确的事件循环
        self._shutdown_event: Optional[asyncio.Event] = None
    
    async def initialize(self) -> bool:
        """初始化所有组件"""
        logger.info("=" * 60)
        logger.info("SPXW 0DTE Trading System V4 - Initializing")
        logger.info("=" * 60)
        
        # 在当前事件循环中创建 Event 和 Lock
        self._shutdown_event = asyncio.Event()
        self._stop_lock = asyncio.Lock()
        self._entry_lock = asyncio.Lock()  # 修复 1: 初始化开仓锁
        
        try:
            # 1. 核心组件
            logger.info("Initializing core components...")
            
            self.event_bus = EventBus()
            set_event_bus(self.event_bus)
            
            self.state = TradingState(self.config.storage.db_path)
            await self.state.initialize()
            set_state(self.state)
            
            self.data_manager = DataManager(
                db_path=self.config.storage.db_path,
                tick_snapshot_path=self.config.storage.tick_snapshot_path,
                historical_data_path=self.config.storage.historical_data_path,
                tick_buffer_size=self.config.storage.tick_buffer_size
            )
            
            # 2. 执行层组件
            logger.info("Initializing execution components...")
            
            self.ib_adapter = IBAdapter(self.config.ibkr, self.event_bus)
            
            self.option_pool = OptionPool(
                self.ib_adapter,
                self.config.option_pool,
                self.event_bus
            )
            
            self.tick_streamer = TickStreamer(
                self.ib_adapter,
                self.config.ibkr.tick,
                self.config.risk.tick_filter,
                self.event_bus
            )
            
            self.option_selector = OptionSelector(
                self.option_pool,
                self.ib_adapter,
                self.config.option_selection
            )
            
            self.order_manager = OrderManager(
                self.ib_adapter,
                self.config.execution,
                self.event_bus,
                self.state
            )
            
            # K 线聚合器 - 支持多周期
            # 从策略配置获取需要的周期 (保持 IBKR 格式如 "5 mins")
            timeframes = [
                self.config.strategy.bar_size,      # 信号周期 (如 "5 mins")
                self.config.strategy.trend_bar_size  # 趋势周期 (如 "15 mins")
            ]
            timeframes = [tf for tf in timeframes if tf]  # 过滤空值
            timeframes = list(set(timeframes))  # 去重
            
            self.bar_aggregator = BarAggregator(
                self.ib_adapter,
                self.event_bus,
                timeframes=timeframes
            )
            logger.info(f"BarAggregator initialized: timeframes={timeframes}")
            
            # 创建交易引擎 (实现 Warm-up 工作流)
            self.trading_engine = TradingEngine(
                self.config,
                self.event_bus,
                self.ib_adapter
            )
            logger.info("TradingEngine initialized")
            
            # 3. 风控层组件
            logger.info("Initializing risk components...")
            
            self.chase_executor = DynamicChaseStopExecutor(
                self.ib_adapter,
                self.config.risk.chase_stop,
                self.event_bus
            )
            
            self.stop_manager = StopManager(
                self.config.risk,
                self.event_bus,
                self.chase_executor,
                self.state
            )
            
            self.circuit_breaker = CircuitBreaker(
                self.config.risk,
                self.event_bus,
                self.state
            )
            
            # 4. 策略层组件
            logger.info("Initializing strategy components...")
            
            self.strategy = ChannelBreakoutStrategy(
                self.config.strategy,
                self.event_bus,
                self.state
            )
            
            # 5. 订阅核心事件
            self._setup_event_handlers()
            
            logger.info("All components initialized successfully")
            return True
            
        except Exception as e:
            logger.error(f"Initialization failed: {e}", exc_info=True)
            return False
    
    def _setup_event_handlers(self) -> None:
        """设置事件处理器"""
        # 信号 -> 订单
        self.event_bus.subscribe(SignalEvent, self._on_signal)
        
        # 成交 -> 持仓管理
        self.event_bus.subscribe(FillEvent, self._on_fill)
        
        # 止损执行结果 -> 清理状态
        self.event_bus.subscribe(StopExecutionResultEvent, self._on_stop_result)
        
        # 连接状态
        self.event_bus.subscribe(ConnectionEvent, self._on_connection)
    
    async def _on_signal(self, event: SignalEvent) -> None:
        """
        处理交易信号 - 严格单持仓控制
        
        ★★★ 修复 2: 用锁保护整个开仓流程 ★★★
        
        规则：
        1. 任何时候不允许同时持有 CALL 和 PUT
        2. 最多只能持有一个持仓
        3. 不重复开仓，反向信号时先平后开
        4. 如果止损正在执行，忽略信号（防止重复平仓产生净空头）
        5. 使用 _entry_lock 防止并发信号竞态条件
        """
        # ★★★ 修复 2: 获取开仓锁，整个流程期间持有 ★★★
        async with self._entry_lock:
            # 检查熔断器
            allowed, reason = self.circuit_breaker.is_trading_allowed()
            if not allowed:
                logger.warning(f"Signal blocked by circuit breaker: {reason}")
                return
            
            logger.info(f"Processing signal: {event.signal_type}")
            
            # 确定信号方向
            signal_direction = "CALL" if event.signal_type == "LONG_CALL" else "PUT"
            
            # ★★★ 修复 2: 使用单一状态源检查当前持仓 ★★★
            # 不再查询数据库，直接使用内存中的状态
            if self._current_position_direction:
                # 已有持仓
                existing_direction = self._current_position_direction
                existing_position_id = self._current_position_id
                
                # ★ 关键检查：如果该持仓正在执行止损，忽略信号
                # 防止止损和反转平仓同时发送卖单，导致净空头
                if existing_position_id and existing_position_id in self.stop_manager.executing_stops:
                    logger.warning(
                        f"Signal ignored: Stop already executing for position {existing_position_id[:8]}... "
                        f"Preventing duplicate sell order that could create naked short position."
                    )
                    return
                
                if existing_direction == signal_direction:
                    # 同方向 → 忽略信号
                    logger.info(f"Signal ignored: Already holding {existing_direction}")
                    return
                else:
                    # 反方向 → 先平仓再开新仓
                    logger.info(f"Reverse signal: Closing {existing_direction} to open {signal_direction}")
                    
                    # 从数据库获取完整持仓信息用于平仓
                    existing_position = self.state.get_position(existing_position_id)
                    if not existing_position:
                        logger.error(f"Position {existing_position_id} not found in state!")
                        # 状态不一致，清理内存状态
                        self._current_position_id = None
                        self._current_position_direction = ""
                        self._current_position_contract = None
                        return
                    
                    # 执行反向平仓
                    closed = await self._close_for_reversal(existing_position)
                    if not closed:
                        logger.error("Failed to close position for reversal, aborting new entry")
                        return
                    
                    # 平仓成功，继续开新仓
            
            # ★★★ 修复 6: 下单前二次确认没有持仓 ★★★
            if self._current_position_id is not None:
                logger.warning(f"Signal aborted: Position opened during processing (race detected)")
                return
            
            # 获取 SPX 价格 (优先使用事件中的价格，其次从引擎获取)
            spx_price = event.spx_price
            if not spx_price and self.trading_engine:
                spx_price = self.trading_engine.state.spx_price
            if not spx_price and self.strategy:
                spx_price = self.strategy.state.spx_price
            
            if not spx_price:
                logger.warning("No SPX price available for option selection")
                return
            
            # 选择期权
            candidate = await self.option_selector.select_option(event, spx_price)
            
            if not candidate:
                logger.warning("No suitable option found for signal")
                return
            
            # ★★★ 修复 6: 选择期权后三次确认状态未变化 ★★★
            if self._current_position_id is not None:
                logger.warning(f"Signal aborted: Position opened during option selection (race detected)")
                return
            
            # 计算入场价格（使用 SPXW tick size 对齐）
            from execution.price_utils import calculate_entry_price
            entry_price = calculate_entry_price(
                mid=candidate.mid,
                buffer=self.config.execution.limit_price_buffer
            )
            
            # ★ 计算手数
            fixed_amount = self.config.execution.fixed_investment_amount
            if fixed_amount > 0:
                # 根据固定投入金额计算手数
                # 手数 = floor(金额 / (价格 * 100))
                quantity = math.floor(fixed_amount / (entry_price * 100))
                # 不到1手按1手
                quantity = max(1, quantity)
                logger.info(
                    f"Position sizing: ${fixed_amount:.0f} / (${entry_price:.2f} × 100) = {quantity} contracts"
                )
            else:
                # 使用固定手数
                quantity = self.config.execution.position_size
            
            logger.info(
                f"Entry price: mid=${candidate.mid:.2f} + buffer=${self.config.execution.limit_price_buffer:.2f} "
                f"-> aligned=${entry_price:.2f}"
            )
            
            # 下单
            order_ctx = await self.order_manager.submit_buy_order(
                contract=candidate.contract,
                quantity=quantity,
                limit_price=entry_price,
                signal_id=str(event.timestamp)
            )
            
            if order_ctx:
                logger.info(f"Order submitted: {order_ctx.order_id}")
    
    async def _close_for_reversal(self, position) -> bool:
        """
        为反向开仓执行平仓
        
        使用 chase_stop_executor 确保成交
        
        ★ 重要：必须先检查是否已有止损在执行，防止重复卖出导致净空头
        
        Returns:
            bool: 平仓是否成功
        """
        logger.info(f"Closing position for reversal: {position.contract_symbol}")
        
        # ★ 关键检查：如果止损已经在执行，等待其完成
        # 防止同一仓位被卖出两次，产生净空头
        if position.id in self.stop_manager.executing_stops:
            logger.warning(
                f"Stop already executing for {position.contract_symbol}, "
                f"waiting for completion instead of sending duplicate sell order"
            )
            # 等待当前止损完成（最多等待 5 秒）
            for i in range(50):
                await asyncio.sleep(0.1)
                if position.id not in self.stop_manager.executing_stops:
                    # 检查持仓是否已被平仓
                    remaining_positions = self.state.get_all_positions()
                    if not remaining_positions or position.id not in [p.id for p in remaining_positions]:
                        logger.info(f"Stop completed for {position.contract_symbol}, position already closed")
                        return True  # 止损已完成平仓，不需要再执行
                    else:
                        logger.warning(f"Stop completed but position still exists, continuing with reversal close")
                        break
            else:
                # 超时
                logger.error(f"Stop execution timeout for {position.contract_symbol} after 5s")
                return False
        
        # 从 stop_manager 获取监控信息
        monitored = self.stop_manager.monitored_positions.get(position.id)
        if not monitored:
            # 可能已经被止损移除
            logger.warning(f"Position {position.id} not found in stop_manager, may have been closed by stop")
            # 再次检查持仓状态
            remaining_positions = self.state.get_all_positions()
            if not remaining_positions or position.id not in [p.id for p in remaining_positions]:
                logger.info(f"Position already closed, reversal close not needed")
                return True
            logger.error(f"Position exists but not in stop_manager: {position.id}")
            return False
        
        # 获取当前价格
        current_price = monitored.last_valid_price
        if not current_price:
            logger.error("No valid price for reversal close")
            return False
        
        # 标记为正在执行（此时已确认没有其他止损在执行）
        self.stop_manager.executing_stops.add(position.id)
        
        try:
            # 构建 PositionStop
            from risk.chase_stop_executor import PositionStop
            position_stop = PositionStop(
                id=position.id,
                contract=monitored.contract,
                contract_id=monitored.contract.conId,
                quantity=position.quantity,
                entry_price=position.entry_price,
                highest_price=monitored.highest_price,
                breakeven_active=monitored.breakeven_active,
                breakeven_price=monitored.breakeven_price,
                trailing_active=monitored.trailing_active
            )
            
            # 使用 chase_executor 执行平仓
            result = await self.chase_executor.execute_stop(position_stop, current_price)
            
            if result.success:
                # 计算 PnL
                pnl = (result.fill_price - position.entry_price) * position.quantity * 100
                pnl_pct = (result.fill_price - position.entry_price) / position.entry_price
                
                logger.info(
                    f"Reversal close complete: {position.contract_symbol} "
                    f"@ ${result.fill_price:.2f} PnL=${pnl:.2f} ({pnl_pct:.1%})"
                )
                
                # 记录交易
                from core.state import Trade
                trade = Trade(
                    position_id=position.id,
                    contract_id=position.contract_id,
                    contract_symbol=position.contract_symbol,
                    direction=position.direction,
                    entry_time=position.entry_time,
                    entry_price=position.entry_price,
                    exit_time=datetime.now(),
                    exit_price=result.fill_price,
                    quantity=position.quantity,
                    realized_pnl=pnl,
                    status="CLOSED"
                )
                trade.realized_pnl_pct = pnl_pct
                await self.state.add_trade(trade)
                
                # 更新熔断器
                await self.circuit_breaker.record_trade_result(pnl, pnl > 0)
                
                # 移除监控
                self.stop_manager.remove_position(position.id)
                
                # 取消 tick 订阅（同步方法）
                self.tick_streamer.unsubscribe(monitored.contract)
                
                # ★ 清理 position_contracts 映射
                self._position_contracts.pop(position.id, None)
                
                # 关闭持仓
                await self.state.close_position(position.id)
                
                # ★★★ 修复 3/4: 原子清理持仓状态（在锁内） ★★★
                self._current_position_id = None
                self._current_position_direction = ""
                self._current_position_contract = None
                logger.info(f"Position state cleared atomically (reversal close)")
                
                # 清除 TradingEngine 方向
                if self.trading_engine:
                    self.trading_engine.state.current_position_direction = ""
                
                return True
            else:
                logger.error(f"Reversal close failed: phase={result.phase}")
                return False
                
        except Exception as e:
            logger.error(f"Error during reversal close: {e}")
            return False
        finally:
            self.stop_manager.executing_stops.discard(position.id)
    
    async def _on_fill(self, event: FillEvent) -> None:
        """
        处理成交
        
        ★★★ 修复 3: 原子更新持仓状态 ★★★
        """
        if event.is_entry:
            # ★★★ 修复 3: 获取锁以原子更新持仓状态 ★★★
            async with self._entry_lock:
                # 新建持仓
                from core.state import Position
                import uuid
                
                # 确定持仓方向
                is_call = event.contract.right == "C"
                direction = "LONG_CALL" if is_call else "LONG_PUT"
                
                position_id = str(uuid.uuid4())
                
                position = Position(
                    id=position_id,
                    contract_id=event.contract.conId,
                    contract_symbol=event.contract_symbol,
                    direction=direction,
                    quantity=event.quantity,
                    entry_price=event.fill_price,
                    current_price=event.fill_price,
                    highest_price=event.fill_price,
                    entry_order_id=event.order_id
                )
                
                await self.state.add_position(position)
                
                # ★★★ 修复 3: 原子更新所有状态 ★★★
                self._current_position_id = position_id
                self._current_position_direction = "CALL" if is_call else "PUT"
                self._current_position_contract = event.contract
                self._position_contracts[position_id] = event.contract
                
                logger.info(
                    f"Position state updated atomically: "
                    f"id={position_id[:8]}... direction={self._current_position_direction}"
                )
                
                # 更新 TradingEngine 的持仓方向状态
                if self.trading_engine:
                    self.trading_engine.state.current_position_direction = self._current_position_direction
                    logger.info(f"TradingEngine position direction: {self._current_position_direction}")
                
                # 添加到止损监控
                self.stop_manager.add_position(position, event.contract)
                
                # 订阅 Tick 数据
                await self.tick_streamer.subscribe(event.contract)
                
                logger.info(f"Position opened and monitored: {event.contract_symbol}")
        else:
            # 平仓
            if event.position_id:
                position = self.state.get_position(event.position_id)
                if position:
                    # 记录交易
                    from core.state import Trade
                    
                    trade = Trade(
                        position_id=event.position_id,
                        contract_id=event.contract.conId,
                        contract_symbol=event.contract_symbol,
                        direction=position.direction,
                        entry_time=position.entry_time,
                        entry_price=position.entry_price,
                        exit_time=datetime.now(),
                        exit_price=event.fill_price,
                        exit_order_id=event.order_id,
                        quantity=event.quantity,
                        realized_pnl=(event.fill_price - position.entry_price) * event.quantity * 100,
                        status="CLOSED"
                    )
                    trade.realized_pnl_pct = (event.fill_price - position.entry_price) / position.entry_price
                    
                    await self.state.add_trade(trade)
                    
                    # 清除 TradingEngine 的持仓方向状态
                    # 检查是否还有其他持仓
                    remaining = self.state.get_all_positions()
                    remaining = [p for p in remaining if p.id != event.position_id]
                    
                    if self.trading_engine:
                        if not remaining:
                            old_direction = self.trading_engine.state.current_position_direction
                            if old_direction:
                                self.trading_engine.state.current_position_direction = ""
                                logger.info(f"Position direction cleared: {old_direction} (exit filled, no remaining)")
                        
                        # 阻止本 bar 继续开仓（等下一根 bar）
                        self.trading_engine.state.signal_blocked_until_next_bar = True
                        logger.info("Signal blocked until next bar (exit filled)")
                    
                    # 更新熔断器
                    await self.circuit_breaker.record_trade_result(
                        trade.realized_pnl,
                        trade.realized_pnl > 0
                    )
    
    async def _on_connection(self, event: ConnectionEvent) -> None:
        """处理连接状态变化"""
        if event.status == "DISCONNECTED":
            logger.warning("Connection lost!")
        elif event.status == "CONNECTED":
            logger.info("Connection restored")
    
    async def _cleanup_position_state_locked(self, position_id: str) -> None:
        """
        ★★★ 修复 4: 统一清理持仓状态（必须在锁内调用） ★★★
        
        原子清理所有与持仓相关的状态:
        - 内存状态 (_current_position_*)
        - TradingEngine 状态
        - position_contracts 映射
        """
        # 只在清理的是当前持仓时才更新状态
        if self._current_position_id == position_id:
            old_direction = self._current_position_direction
            self._current_position_id = None
            self._current_position_direction = ""
            self._current_position_contract = None
            logger.info(
                f"Position state cleared atomically: "
                f"id={position_id[:8]}... direction={old_direction}"
            )
        
        # 清理合约映射
        self._position_contracts.pop(position_id, None)
        
        # 清理 TradingEngine 状态
        if self.trading_engine:
            # 检查是否还有其他持仓
            remaining = self.state.get_all_positions()
            remaining = [p for p in remaining if p.id != position_id]
            
            if not remaining:
                self.trading_engine.state.current_position_direction = ""
                self.trading_engine.state.in_breakout = False
                self.trading_engine.state.last_signal_direction = ""
            
            self.trading_engine.state.signal_blocked_until_next_bar = True
    
    async def _on_stop_result(self, event: StopExecutionResultEvent) -> None:
        """处理止损执行结果"""
        pnl_str = f"pnl=${event.pnl:.2f} ({event.pnl_pct:.1%})" if event.pnl else "pnl=N/A"
        logger.info(
            f"Stop result: position={event.position_id[:8]}... "
            f"phase={event.phase} success={event.success} {pnl_str}"
        )
        
        if not event.success:
            # ★ 止损失败，检查是否有遗留订单已成交
            logger.error(
                f"Stop execution failed for {event.position_id[:8]}..., "
                f"phase={event.phase}. Checking for filled orders..."
            )
            
            contract = self._position_contracts.get(event.position_id)
            if contract:
                # 检查是否有该合约的已成交卖单
                for trade in self.ib_adapter.ib.trades():
                    if (trade.contract.conId == contract.conId and 
                        trade.order.action == "SELL" and
                        trade.orderStatus.status == "Filled"):
                        
                        logger.warning(
                            f"⚠️ Found filled sell order during failed stop! "
                            f"Fill price=${trade.orderStatus.avgFillPrice:.2f}. "
                            f"Treating as success and cleaning up..."
                        )
                        
                        # ★★★ 修复 4: 使用统一清理方法（在锁内） ★★★
                        async with self._entry_lock:
                            # 取消 tick 订阅
                            self.tick_streamer.unsubscribe(contract)
                            
                            # 原子清理状态
                            await self._cleanup_position_state_locked(event.position_id)
                            
                            # 移除监控
                            self.stop_manager.remove_position(event.position_id)
                            await self.state.close_position(event.position_id)
                        
                        logger.info("Cleaned up after finding filled order in failed stop")
                        return
            
            # 如果没有成交的订单，继续监控等待重试
            logger.info("No filled orders found, position remains monitored for retry")
            return
        
        if event.success:
            # ★★★ 修复 4: 在锁内原子清理所有状态 ★★★
            async with self._entry_lock:
                # 获取持仓信息（在关闭前获取）
                position = self.state.get_position(event.position_id)
                if position:
                    # 记录交易
                    from core.state import Trade
                    
                    trade = Trade(
                        position_id=event.position_id,
                        contract_id=position.contract_id,
                        contract_symbol=position.contract_symbol,
                        direction=position.direction,
                        entry_time=position.entry_time,
                        entry_price=position.entry_price,
                        exit_time=datetime.now(),
                        exit_price=event.fill_price or 0,
                        quantity=position.quantity,
                        realized_pnl=event.pnl or 0,
                        status="CLOSED"
                    )
                    trade.realized_pnl_pct = event.pnl_pct or 0
                    
                    await self.state.add_trade(trade)
                    
                    # 更新熔断器
                    await self.circuit_breaker.record_trade_result(
                        trade.realized_pnl,
                        trade.realized_pnl > 0
                    )
                
                # ★ 取消 tick 订阅（使用保存的合约映射）
                contract = self._position_contracts.get(event.position_id)
                if contract:
                    self.tick_streamer.unsubscribe(contract)
                    logger.info(f"Tick subscription cancelled: {contract.localSymbol}")
                
                # ★★★ 修复 4: 使用统一清理方法 ★★★
                await self._cleanup_position_state_locked(event.position_id)
                
                # 移除监控和关闭持仓
                self.stop_manager.remove_position(event.position_id)
                await self.state.close_position(event.position_id)
                
                logger.info(f"Position closed and state cleaned up atomically (stop executed)")
    
    async def _verify_position_consistency(self) -> None:
        """
        ★ 修复 10: 启动时验证持仓一致性
        
        检查 DB、TWS 和内存三者的持仓状态是否一致，并尝试同步。
        """
        logger.info("=" * 60)
        logger.info("Verifying position consistency...")
        logger.info("=" * 60)
        
        # 1. 获取本地数据库持仓
        db_positions = self.state.get_all_positions()
        logger.info(f"DB positions: {len(db_positions)}")
        for pos in db_positions:
            logger.info(f"  - {pos.contract_symbol} ({pos.direction}): {pos.quantity} @ ${pos.entry_price:.2f}")
        
        # 2. 获取 TWS 实际持仓（过滤 SPXW 期权）
        tws_positions = await self.ib_adapter.get_positions()
        spxw_tws_positions = [
            p for p in tws_positions
            if hasattr(p.contract, 'tradingClass') and p.contract.tradingClass == 'SPXW'
        ]
        logger.info(f"TWS SPXW positions: {len(spxw_tws_positions)}")
        for pos in spxw_tws_positions:
            logger.info(f"  - {pos.contract.localSymbol}: {pos.position} shares")
        
        # 3. 获取内存状态
        memory_position_id = self._current_position_id
        memory_direction = self._current_position_direction
        logger.info(f"Memory position: id={memory_position_id[:8] if memory_position_id else 'None'}... direction={memory_direction or 'None'}")
        
        # 4. 检测不一致并尝试修复
        inconsistencies = []
        
        # 检查：DB 有持仓但内存为空
        if db_positions and not memory_position_id:
            inconsistencies.append("DB has positions but memory is empty")
            logger.warning("⚠️ Inconsistency: DB has positions but memory is empty, restoring from DB...")
            
            # 从 DB 恢复（只恢复第一个，因为系统设计为单持仓）
            if len(db_positions) > 1:
                logger.error(f"❌ CRITICAL: Multiple positions in DB ({len(db_positions)}), violates single-position constraint!")
                inconsistencies.append(f"Multiple DB positions: {len(db_positions)}")
                
                # 发布错误事件
                await self.event_bus.publish(ErrorEvent(
                    error_type="POSITION_CONSISTENCY",
                    error_message=f"Multiple positions in DB: {len(db_positions)}",
                    component="TradingSystem",
                    severity="CRITICAL"
                ))
            else:
                position = db_positions[0]
                self._current_position_id = position.id
                self._current_position_direction = "CALL" if "CALL" in position.direction else "PUT"
                logger.info(f"✅ Restored memory state from DB: {position.contract_symbol} ({self._current_position_direction})")
        
        # 检查：内存有持仓但 DB 为空
        elif memory_position_id and not db_positions:
            inconsistencies.append("Memory has position but DB is empty")
            logger.warning("⚠️ Inconsistency: Memory has position but DB is empty, clearing memory...")
            self._current_position_id = None
            self._current_position_direction = ""
            self._current_position_contract = None
            logger.info("✅ Cleared memory state")
        
        # 检查：DB 有持仓但 TWS 没有
        if db_positions and not spxw_tws_positions:
            inconsistencies.append("DB has positions but TWS has none")
            logger.warning(
                "⚠️ Warning: DB has positions but TWS has none. "
                "Position may have expired or been closed externally."
            )
            
            # 发布警告事件
            await self.event_bus.publish(ErrorEvent(
                error_type="POSITION_CONSISTENCY",
                error_message="DB has positions but TWS has none (may have expired)",
                component="TradingSystem",
                severity="WARNING"
            ))
        
        # 检查：TWS 有持仓但 DB 没有
        if spxw_tws_positions and not db_positions:
            inconsistencies.append("TWS has positions but DB has none")
            logger.warning(
                "⚠️ Warning: TWS has positions but DB has none. "
                "Position may have been opened externally or DB is out of sync."
            )
            
            # 发布警告事件
            await self.event_bus.publish(ErrorEvent(
                error_type="POSITION_CONSISTENCY",
                error_message="TWS has positions but DB has none (external position?)",
                component="TradingSystem",
                severity="WARNING"
            ))
        
        # 检查：TWS 有多个 SPXW 持仓
        if len(spxw_tws_positions) > 1:
            inconsistencies.append(f"Multiple TWS SPXW positions: {len(spxw_tws_positions)}")
            logger.error(
                f"❌ CRITICAL: TWS has {len(spxw_tws_positions)} SPXW positions, "
                f"violates single-position constraint!"
            )
            
            # 发布错误事件
            await self.event_bus.publish(ErrorEvent(
                error_type="POSITION_CONSISTENCY",
                error_message=f"Multiple TWS SPXW positions: {len(spxw_tws_positions)}",
                component="TradingSystem",
                severity="CRITICAL"
            ))
        
        # 5. 同步 TradingEngine 状态
        if self.trading_engine:
            if memory_direction:
                self.trading_engine.state.current_position_direction = memory_direction
                logger.info(f"Synced TradingEngine direction: {memory_direction}")
            else:
                self.trading_engine.state.current_position_direction = ""
                logger.info("TradingEngine direction cleared")
        
        # 6. 总结
        if inconsistencies:
            logger.warning(f"⚠️ Found {len(inconsistencies)} inconsistenc{'y' if len(inconsistencies) == 1 else 'ies'}:")
            for inc in inconsistencies:
                logger.warning(f"  - {inc}")
        else:
            logger.info("✅ Position consistency verified: all systems in sync")
        
        logger.info("=" * 60)
    
    async def start(self) -> None:
        """启动交易系统 - 使用 Warm-up 工作流"""
        if self._running:
            logger.warning("System already running")
            return
        
        logger.info("=" * 60)
        logger.info("Starting trading system with Warm-up workflow...")
        logger.info("=" * 60)
        
        # 检查交易日历状态
        calendar = get_trading_calendar()
        status = calendar.get_trading_status()
        
        logger.info(f"Current time (ET): {status['current_time_et']}")
        logger.info(f"Is trading day: {status['is_trading_day']}")
        logger.info(f"Is market open: {status['is_market_open']}")
        logger.info(f"0DTE expiry: {status['0dte_expiry']}")
        logger.info(f"Trading status: {status['reason']}")
        
        # ================================================================
        # Phase 1: WARM-UP (由 TradingEngine 执行)
        # - 连接 IBKR
        # - 下载历史数据
        # - 预计算通道和趋势
        # ================================================================
        if not await self.trading_engine.warmup():
            logger.error("Warm-up phase failed")
            return
        
        # ★ 修复 10: 启动时验证持仓一致性
        await self._verify_position_consistency()
        
        # 从引擎获取 SPX 合约
        self._spx_contract = self.trading_engine._spx_contract
        
        # 获取初始 SPX 价格
        spx_price = None
        if self._spx_contract:
            # 尝试获取价格
            ticker = await self.ib_adapter.subscribe_market_data(self._spx_contract)
            if ticker:
                await asyncio.sleep(1)
                import math
                spx_price = ticker.last
                if spx_price is None or (isinstance(spx_price, float) and math.isnan(spx_price)):
                    spx_price = ticker.close
                if spx_price is None or (isinstance(spx_price, float) and math.isnan(spx_price)):
                    # 从引擎状态获取
                    spx_price = self.trading_engine.state.spx_price
            
            if spx_price and spx_price > 0:
                logger.info(f"SPX price: ${spx_price:.2f}")
                await self.option_pool.ensure_ready(spx_price)
            else:
                # 从引擎的历史数据获取
                if not self.trading_engine.state.signal_bars.empty:
                    spx_price = self.trading_engine.state.signal_bars['close'].iloc[-1]
                    logger.info(f"Using historical SPX price: ${spx_price:.2f}")
                    await self.option_pool.ensure_ready(spx_price)
        
        # 将历史数据同步到策略 (如果需要)
        if self.strategy and not self.trading_engine.state.signal_bars.empty:
            self.strategy.load_historical_bars(self.trading_engine.state.signal_bars)
        
        # ================================================================
        # Phase 2: SUBSCRIBE EARLY (由 TradingEngine 执行)
        # - 订阅 5 秒实时 K 线
        # - 订阅 Tick 数据
        # ================================================================
        if not await self.trading_engine.subscribe_early():
            logger.error("Early subscription failed")
            return
        
        # 信号由 TradingEngine 发布到 event_bus，已在 _setup_event_handlers 中订阅
        
        # 启动 Tick 流监控 (用于止损)
        await self.tick_streamer.start()
        
        # ================================================================
        # Phase 3: START TRADING (由 TradingEngine 执行)
        # - 使用预计算的通道和趋势
        # - 第一个 Tick 到达时立即可以交易
        # ================================================================
        await self.trading_engine.start_trading()
        
        self._running = True
        
        # 发布系统状态
        await self.event_bus.publish(SystemStatusEvent(
            status="RUNNING",
            message="Trading system started"
        ))
        
        logger.info("=" * 60)
        logger.info("Trading system RUNNING")
        logger.info("=" * 60)
        
        # 主循环
        await self._main_loop()
    
    async def _main_loop(self) -> None:
        """主事件循环"""
        eod_check_interval = 30  # 每 30 秒检查一次 EOD
        last_eod_check = 0
        
        while self._running:
            try:
                # 让 IB 处理事件
                await self.ib_adapter.sleep(0.1)
                
                # 检查是否该关闭
                if self._shutdown_event is not None and self._shutdown_event.is_set():
                    break
                
                # 定期检查 EOD 强制平仓
                import time
                now = time.time()
                if now - last_eod_check >= eod_check_interval:
                    last_eod_check = now
                    await self._check_and_execute_eod_close()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                await asyncio.sleep(1)
    
    async def _check_and_execute_eod_close(self) -> None:
        """
        检查是否需要 EOD 强制平仓
        
        在收盘前 market_close_buffer_minutes 分钟内，强制平掉所有持仓
        """
        # 如果已经触发过 EOD 平仓，不再重复
        if self._eod_close_triggered:
            return
        
        from core.calendar import get_trading_calendar
        
        calendar = get_trading_calendar()
        
        # 获取距离收盘的秒数
        seconds_to_close = calendar.seconds_to_market_close()
        if seconds_to_close is None or seconds_to_close < 0:
            return  # 不是交易日或已收盘
        
        buffer_seconds = self.config.risk.market_close_buffer_minutes * 60
        
        # 如果距离收盘时间 <= buffer，触发 EOD 平仓
        if seconds_to_close <= buffer_seconds:
            positions = self.state.get_all_positions()
            minutes_left = seconds_to_close / 60
            
            if not positions:
                # 没有持仓，记录并触发退出
                if not self._eod_close_triggered:
                    self._eod_close_triggered = True
                    logger.info(f"⏰ EOD window active ({minutes_left:.1f} min to close) - No positions to close")
                    # 无持仓也触发优雅退出
                    await self._trigger_eod_shutdown()
                return
            
            # 标记 EOD 已触发
            self._eod_close_triggered = True
            
            logger.warning(
                f"⏰ EOD CLOSE TRIGGERED: {minutes_left:.1f} minutes to market close, "
                f"force closing {len(positions)} position(s)"
            )
            
            # 对每个持仓执行止损
            for position in positions:
                if position.id in self.stop_manager.executing_stops:
                    logger.info(f"Position {position.id[:8]}... already executing stop")
                    continue
                
                # 获取监控中的持仓信息
                monitored = self.stop_manager.monitored_positions.get(position.id)
                if not monitored:
                    logger.warning(f"Position {position.id[:8]}... not in stop monitoring")
                    continue
                
                # 获取当前价格
                current_price = monitored.last_valid_price
                if not current_price:
                    logger.warning(f"No valid price for {position.contract_symbol}")
                    continue
                
                pnl_pct = (current_price - position.entry_price) / position.entry_price
                logger.info(
                    f"EOD closing: {position.contract_symbol} "
                    f"Entry=${position.entry_price:.2f} Current=${current_price:.2f} "
                    f"PnL={pnl_pct:.1%}"
                )
                
                # 使用 chase_stop_executor 执行平仓
                from risk.chase_stop_executor import PositionStop
                
                position_stop = PositionStop(
                    id=position.id,
                    contract=monitored.contract,
                    contract_id=monitored.contract.conId,
                    quantity=position.quantity,
                    entry_price=position.entry_price,
                    highest_price=monitored.highest_price,
                    breakeven_active=monitored.breakeven_active,
                    breakeven_price=monitored.breakeven_price,
                    trailing_active=monitored.trailing_active
                )
                
                # 标记正在执行
                self.stop_manager.executing_stops.add(position.id)
                
                try:
                    result = await self.chase_executor.execute_stop(
                        position_stop,
                        current_price
                    )
                    
                    if result.success:
                        self.stop_manager.remove_position(position.id)
                        await self.state.close_position(position.id)
                        logger.info(f"✅ EOD close complete: {position.contract_symbol}")
                    else:
                        logger.error(f"❌ EOD close failed: {position.contract_symbol} phase={result.phase}")
                finally:
                    self.stop_manager.executing_stops.discard(position.id)
            
            # EOD 平仓完成后，触发优雅退出
            await self._trigger_eod_shutdown()
    
    async def _trigger_eod_shutdown(self) -> None:
        """EOD 平仓完成后触发优雅退出"""
        logger.info("=" * 60)
        logger.info("🏁 EOD CLOSE COMPLETE - Initiating graceful shutdown")
        logger.info("=" * 60)
        
        # 等待一小段时间确保所有事件处理完成
        await asyncio.sleep(2)
        
        # 触发系统关闭
        await self.stop()
        
        # 如果是 NiceGUI 模式，需要关闭应用
        try:
            from nicegui import app
            logger.info("Shutting down NiceGUI application...")
            app.shutdown()
        except Exception as e:
            logger.debug(f"NiceGUI shutdown: {e}")
        
        logger.info("EOD shutdown complete")
    
    async def stop(self) -> None:
        """停止交易系统"""
        # 使用锁防止并发调用
        if self._stop_lock:
            async with self._stop_lock:
                await self._do_stop()
        else:
            await self._do_stop()
    
    async def _do_stop(self) -> None:
        """实际执行停止操作"""
        # 防止重复调用
        if self._stopped:
            logger.debug("stop() already called, skipping")
            return
        self._stopped = True
        
        logger.info("Stopping trading system...")
        
        self._running = False
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        
        # 停止交易引擎
        if self.trading_engine:
            await self.trading_engine.stop()
        
        # 停止 K 线聚合器
        if self.bar_aggregator:
            await self.bar_aggregator.stop()
        
        # 停止组件
        if self.tick_streamer:
            await self.tick_streamer.stop()
        
        # 断开连接
        if self.ib_adapter:
            await self.ib_adapter.disconnect()
        
        # 关闭数据库
        if self.state:
            await self.state.close()
        
        await self.event_bus.publish(SystemStatusEvent(
            status="STOPPED",
            message="Trading system stopped"
        ))
        
        logger.info("Trading system stopped")
    
    def request_shutdown(self) -> None:
        """请求关闭"""
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        self._running = False


async def main():
    """主函数（无 UI 模式）"""
    from core import get_config
    
    config = get_config()
    
    # 创建交易系统
    system = TradingSystem(config)
    
    # 设置信号处理 (仅 Unix)
    import platform
    if platform.system() != "Windows":
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, system.request_shutdown)
    
    # 初始化
    if not await system.initialize():
        logger.error("System initialization failed")
        return
    
    # 标准模式：直接运行交易系统
    try:
        await system.start()
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    finally:
        await system.stop()


async def run_trading_loop(system: TradingSystem) -> None:
    """运行交易循环（用于集成 UI 模式）"""
    try:
        await system.start()
    except asyncio.CancelledError:
        logger.info("Trading loop cancelled")
    except Exception as e:
        logger.error(f"Trading loop error: {e}")
    # 注意：不在这里调用 stop()，由 on_shutdown 统一处理


def main_with_ui(args, config) -> None:
    """使用 NiceGUI 集成模式运行"""
    from nicegui import app, ui
    
    # ========================================================================
    # 关键：不在 ui.run() 之前创建任何 asyncio 相关对象
    # 所有初始化必须在 app.on_startup 回调中进行
    # ========================================================================
    
    # 只保存配置到 app.state，不创建任何对象
    app.state.config = config
    app.state.system = None
    app.state.trading_task = None
    
    # 在 NiceGUI 启动后初始化所有内容
    @app.on_startup
    async def startup():
        logger.info("=" * 60)
        logger.info("Initializing trading system in NiceGUI event loop...")
        logger.info("=" * 60)
        
        # ====================================================================
        # 关键：强制 ib_insync 使用当前运行的事件循环
        # ib_insync 内部使用 asyncio.get_event_loop() 而不是 get_running_loop()
        # 我们需要确保它们返回相同的循环
        # ====================================================================
        loop = asyncio.get_running_loop()
        asyncio.set_event_loop(loop)
        
        # 现在才创建 TradingSystem（在正确的事件循环中）
        system = TradingSystem(app.state.config)
        app.state.system = system
        
        if await system.initialize():
            logger.info("Starting trading system...")
            app.state.trading_task = asyncio.create_task(run_trading_loop(system))
        else:
            logger.error("System initialization failed")
    
    @app.on_shutdown
    async def shutdown():
        logger.info("Shutting down trading system... (on_shutdown called)")
        
        # 先取消交易任务
        if app.state.trading_task:
            logger.debug("Cancelling trading task...")
            app.state.trading_task.cancel()
            try:
                await app.state.trading_task
            except asyncio.CancelledError:
                pass
            logger.debug("Trading task cancelled")
        
        # 然后优雅关闭系统（会设置 _shutdown 标志并断开连接）
        if app.state.system:
            logger.debug("Calling system.stop()...")
            await app.state.system.stop()
            logger.debug("system.stop() completed")
        
        logger.info("Trading system shutdown complete")
    
    # ========================================================================
    # 定义 UI 页面（使用 app.state.system 获取系统引用）
    # ========================================================================
    
    @ui.page('/')
    async def main_page():
        await create_dashboard_page()
    
    @ui.page('/settings')
    async def settings_page():
        await create_settings_page()
    
    logger.info("=" * 60)
    logger.info(f"Starting integrated mode with Web UI")
    logger.info(f"Open http://localhost:{args.ui_port} in browser")
    logger.info("=" * 60)
    
    # 运行 NiceGUI
    ui.run(
        host="0.0.0.0",
        port=args.ui_port,
        title="SPXW Trading Dashboard",
        dark=True,
        reload=False,
        show=False
    )


async def create_dashboard_page():
    """创建仪表盘页面"""
    from nicegui import ui, app
    
    ui.dark_mode().enable()
    
    with ui.header().classes('bg-gray-900 text-white'):
        with ui.row().classes('w-full items-center'):
            ui.label('SPXW 0DTE Trading System').classes('text-xl font-bold')
            ui.space()
            ui.button('Dashboard', on_click=lambda: ui.navigate.to('/')).props('flat color=white')
            ui.button('Settings', on_click=lambda: ui.navigate.to('/settings')).props('flat color=white')
    
    with ui.column().classes('w-full p-4 gap-4'):
        # 状态卡片行
        with ui.row().classes('w-full gap-4'):
            with ui.card().classes('bg-gray-800 flex-1'):
                ui.label('System Status').classes('text-gray-400 text-sm')
                status_label = ui.label('INITIALIZING').classes('text-2xl font-bold text-yellow-400')
            
            with ui.card().classes('bg-gray-800 flex-1'):
                ui.label('Today P&L').classes('text-gray-400 text-sm')
                pnl_label = ui.label('$0.00').classes('text-2xl font-bold text-white')
            
            with ui.card().classes('bg-gray-800 flex-1'):
                ui.label('Open Positions').classes('text-gray-400 text-sm')
                positions_label = ui.label('0').classes('text-2xl font-bold text-white')
            
            with ui.card().classes('bg-gray-800 flex-1'):
                ui.label('Today Trades').classes('text-gray-400 text-sm')
                trades_label = ui.label('0').classes('text-2xl font-bold text-white')
        
        # 图表和持仓
        with ui.row().classes('w-full gap-4'):
            with ui.card().classes('bg-gray-800 flex-1'):
                ui.label('Equity Curve').classes('text-gray-400 text-sm mb-2')
                ui.echart({
                    'backgroundColor': 'transparent',
                    'xAxis': {'type': 'category', 'data': [], 'axisLine': {'lineStyle': {'color': '#666'}}},
                    'yAxis': {'type': 'value', 'axisLine': {'lineStyle': {'color': '#666'}}, 'splitLine': {'lineStyle': {'color': '#333'}}},
                    'series': [{'type': 'line', 'data': [], 'smooth': True, 'areaStyle': {'opacity': 0.3}, 'lineStyle': {'color': '#4ade80'}, 'itemStyle': {'color': '#4ade80'}}],
                    'tooltip': {'trigger': 'axis'},
                    'grid': {'left': '10%', 'right': '5%', 'top': '10%', 'bottom': '15%'}
                }).classes('w-full h-64')
            
            with ui.card().classes('bg-gray-800 flex-1'):
                ui.label('Open Positions').classes('text-gray-400 text-sm mb-2')
                positions_table = ui.table(
                    columns=[
                        {'name': 'contract', 'label': 'Contract', 'field': 'contract', 'align': 'left'},
                        {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'align': 'center'},
                        {'name': 'entry', 'label': 'Entry', 'field': 'entry', 'align': 'right'},
                        {'name': 'current', 'label': 'Current', 'field': 'current', 'align': 'right'},
                        {'name': 'pnl', 'label': 'P&L', 'field': 'pnl', 'align': 'right'},
                    ],
                    rows=[],
                    row_key='contract'
                ).classes('w-full').props('dark dense')
        
        # 最近交易
        with ui.card().classes('bg-gray-800 w-full'):
            ui.label('Recent Trades').classes('text-gray-400 text-sm mb-2')
            trades_table = ui.table(
                columns=[
                    {'name': 'time', 'label': 'Time', 'field': 'time', 'align': 'left'},
                    {'name': 'contract', 'label': 'Contract', 'field': 'contract', 'align': 'left'},
                    {'name': 'action', 'label': 'Action', 'field': 'action', 'align': 'center'},
                    {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'align': 'center'},
                    {'name': 'price', 'label': 'Price', 'field': 'price', 'align': 'right'},
                    {'name': 'pnl', 'label': 'P&L', 'field': 'pnl', 'align': 'right'},
                ],
                rows=[],
                row_key='time'
            ).classes('w-full').props('dark dense')
        
        # 日志区域
        with ui.card().classes('bg-gray-800 w-full'):
            ui.label('System Log').classes('text-gray-400 text-sm mb-2')
            log_area = ui.log(max_lines=20).classes('w-full h-32 bg-gray-900 text-green-400 font-mono text-sm')
    
    # 定时更新
    async def update_dashboard():
        system = getattr(app.state, 'system', None)
        
        if system is None:
            status_label.text = 'INITIALIZING'
            status_label.classes('text-2xl font-bold text-yellow-400', remove='text-green-400 text-red-400')
            return
        
        # 更新状态
        if hasattr(system, '_running') and system._running:
            status_label.text = 'RUNNING'
            status_label.classes('text-2xl font-bold text-green-400', remove='text-yellow-400 text-red-400')
        else:
            status_label.text = 'STOPPED'
            status_label.classes('text-2xl font-bold text-red-400', remove='text-yellow-400 text-green-400')
        
        # 更新 PnL
        daily_pnl = 0.0
        if hasattr(system, 'state') and system.state:
            if hasattr(system.state, 'get_today_pnl'):
                daily_pnl = system.state.get_today_pnl()
        color = 'text-green-400' if daily_pnl >= 0 else 'text-red-400'
        pnl_label.text = f'${daily_pnl:,.2f}'
        pnl_label.classes(f'text-2xl font-bold {color}', remove='text-white text-green-400 text-red-400')
        
        # 更新持仓数
        pos_count = 0
        if hasattr(system, 'stop_manager') and system.stop_manager:
            if hasattr(system.stop_manager, 'get_monitored_positions'):
                pos_count = len(system.stop_manager.get_monitored_positions())
        positions_label.text = str(pos_count)
        
        # 更新交易数
        trade_count = 0
        if hasattr(system, 'state') and system.state:
            if hasattr(system.state, 'get_today_trades'):
                trade_count = len(system.state.get_today_trades())
        trades_label.text = str(trade_count)
    
    ui.timer(1.0, update_dashboard)


async def create_settings_page():
    """创建设置页面"""
    from nicegui import ui, app
    
    ui.dark_mode().enable()
    
    with ui.header().classes('bg-gray-900 text-white'):
        with ui.row().classes('w-full items-center'):
            ui.label('Settings').classes('text-xl font-bold')
            ui.space()
            ui.button('Dashboard', on_click=lambda: ui.navigate.to('/')).props('flat color=white')
            ui.button('Settings', on_click=lambda: ui.navigate.to('/settings')).props('flat color=white')
    
    with ui.column().classes('w-full p-4 gap-4'):
        with ui.card().classes('bg-gray-800 w-full'):
            ui.label('Trading Control').classes('text-xl text-white mb-4')
            
            with ui.row().classes('gap-4'):
                def stop_trading():
                    system = getattr(app.state, 'system', None)
                    if system:
                        system.request_shutdown()
                        ui.notify('Trading stopped', type='warning')
                    else:
                        ui.notify('No trading system', type='negative')
                
                def emergency_stop():
                    system = getattr(app.state, 'system', None)
                    if system:
                        system.request_shutdown()
                        ui.notify('EMERGENCY STOP!', type='negative')
                    else:
                        ui.notify('No trading system', type='negative')
                
                ui.button('Stop Trading', on_click=stop_trading).props('color=orange')
                ui.button('Emergency Stop', on_click=emergency_stop).props('color=red')
        
        with ui.card().classes('bg-gray-800 w-full'):
            ui.label('System Info').classes('text-xl text-white mb-4')
            
            with ui.column().classes('gap-2'):
                ui.label('Version: V4.0').classes('text-gray-300')
                ui.label('Mode: Paper Trading').classes('text-gray-300')
                
                async def check_connection():
                    system = getattr(app.state, 'system', None)
                    if system and hasattr(system, 'ib_adapter') and system.ib_adapter:
                        if system.ib_adapter.is_connected:
                            return 'Connected'
                    return 'Disconnected'
                
                conn_label = ui.label('Connection: Checking...').classes('text-gray-300')
                
                async def update_connection():
                    status = await check_connection()
                    conn_label.text = f'Connection: {status}'
                
                ui.timer(2.0, update_connection)


if __name__ == "__main__":
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="SPXW 0DTE Trading System V4")
    parser.add_argument(
        "--config",
        type=str,
        default="config/settings.yaml",
        help="Configuration file path"
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["paper", "live"],
        help="Trading mode (overrides config)"
    )
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Enable Web UI dashboard"
    )
    parser.add_argument(
        "--ui-port",
        type=int,
        default=8080,
        help="Web UI port (default: 8080)"
    )
    args = parser.parse_args()
    
    # 加载配置
    try:
        config = load_config(args.config)
        if args.mode:
            config.system.mode = args.mode
        set_config(config)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        sys.exit(1)
    
    # 验证配置
    if config.system.mode == "live":
        config.validate_for_mode()
    
    # 根据模式运行
    try:
        if args.ui:
            main_with_ui(args, config)
        else:
            asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received Ctrl+C, exiting...")
    except SystemExit:
        pass
