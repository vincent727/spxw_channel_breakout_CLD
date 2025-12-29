"""
回测引擎模块

SPXW 0DTE 期权自动交易系统 V4

特性:
1. 复用实盘策略代码
2. 模拟经纪商执行
3. 模拟追单止损
4. 完整绩效统计
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Callable
from pathlib import Path

import pandas as pd
import numpy as np

from core.event_bus import EventBus
from core.events import (
    BarEvent, TickEvent, SignalEvent, FillEvent,
    StopTriggeredEvent, StopExecutionResultEvent,
    TradeClosedEvent
)
from core.config import BacktestConfig, TradingConfig
from strategy.channel_breakout import ChannelBreakoutStrategy
from analytics.performance_tracker import PerformanceTracker

logger = logging.getLogger(__name__)


@dataclass
class BacktestPosition:
    """回测持仓"""
    id: str
    symbol: str
    direction: str  # LONG_CALL or LONG_PUT
    quantity: int
    entry_price: float
    entry_time: datetime
    current_price: float
    highest_price: float
    stop_price: float = 0.0
    breakeven_active: bool = False
    trailing_active: bool = False


@dataclass
class BacktestTrade:
    """回测交易记录"""
    trade_id: str
    symbol: str
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    stop_type: str
    commission: float


@dataclass
class BacktestResult:
    """回测结果"""
    start_date: str
    end_date: str
    initial_capital: float
    final_capital: float
    total_return: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    avg_trade_pnl: float
    avg_holding_time: float
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)


class SimulatedBroker:
    """
    模拟经纪商
    
    模拟订单执行、滑点、佣金
    """
    
    def __init__(
        self,
        commission_per_contract: float = 0.65,
        slippage_pct: float = 0.01,
        fill_probability: float = 0.95
    ):
        self.commission = commission_per_contract
        self.slippage_pct = slippage_pct
        self.fill_probability = fill_probability
        
        self.orders: List[Dict] = []
        self.fills: List[Dict] = []
    
    def submit_order(
        self,
        symbol: str,
        action: str,
        quantity: int,
        limit_price: float,
        current_bid: float,
        current_ask: float
    ) -> Optional[Dict]:
        """
        提交订单
        
        Returns:
            成交信息或 None
        """
        # 模拟成交概率
        if np.random.random() > self.fill_probability:
            return None
        
        # 计算成交价
        if action == "BUY":
            # 买入以 Ask 成交，加滑点
            fill_price = current_ask * (1 + self.slippage_pct)
        else:
            # 卖出以 Bid 成交，减滑点
            fill_price = current_bid * (1 - self.slippage_pct)
        
        # 计算佣金
        commission = self.commission * quantity
        
        fill = {
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "fill_price": fill_price,
            "commission": commission,
            "timestamp": datetime.now()
        }
        
        self.fills.append(fill)
        
        return fill
    
    def simulate_chase_stop(
        self,
        entry_price: float,
        trigger_price: float,
        bid_prices: List[float]
    ) -> Dict:
        """
        模拟追单止损
        
        简化模拟：根据价格序列模拟追单过程
        """
        chase_count = 0
        fill_price = None
        phase = "AGGRESSIVE"
        
        for i, bid in enumerate(bid_prices):
            # 模拟 Phase 1
            if i == 0:
                if np.random.random() < 0.3:  # 30% 概率立即成交
                    fill_price = bid * 0.98
                    phase = "AGGRESSIVE"
                    break
            
            # 模拟 Phase 2 追单
            elif i < 4:
                chase_count += 1
                if np.random.random() < 0.5:  # 50% 概率成交
                    fill_price = bid * 0.97
                    phase = "CHASING"
                    break
            
            # 模拟 Phase 3
            else:
                if bid <= 0.20:
                    phase = "ABANDONED"
                    fill_price = None
                    break
                else:
                    fill_price = bid * 0.85
                    phase = "PANIC"
                    break
        
        return {
            "fill_price": fill_price,
            "phase": phase,
            "chase_count": chase_count,
            "slippage": (entry_price - fill_price) / entry_price if fill_price else 0
        }


class BacktestEngine:
    """
    回测引擎
    
    特性:
    1. 事件驱动，复用策略代码
    2. 模拟真实执行
    3. 完整绩效统计
    """
    
    def __init__(self, config: BacktestConfig, trading_config: TradingConfig):
        self.config = config
        self.trading_config = trading_config
        
        # 事件总线
        self.event_bus = EventBus()
        
        # 模拟经纪商
        self.broker = SimulatedBroker(
            commission_per_contract=config.commission_per_contract,
            slippage_pct=config.slippage_pct
        )
        
        # 策略
        self.strategy: Optional[ChannelBreakoutStrategy] = None
        
        # 持仓和交易
        self.positions: Dict[str, BacktestPosition] = {}
        self.trades: List[BacktestTrade] = []
        
        # 资金曲线
        self.initial_capital = config.initial_capital
        self.capital = config.initial_capital
        self.equity_curve: List[float] = [config.initial_capital]
        
        # 数据
        self.bar_data: pd.DataFrame = pd.DataFrame()
        self.option_data: Dict[str, pd.DataFrame] = {}
        
        # 当前回测状态
        self.current_time: Optional[datetime] = None
        self.current_bar: Optional[pd.Series] = None
        
        # 设置事件处理
        self._setup_event_handlers()
        
        logger.info(
            f"BacktestEngine initialized: "
            f"capital=${config.initial_capital}, "
            f"commission=${config.commission_per_contract}"
        )
    
    def _setup_event_handlers(self) -> None:
        """设置事件处理"""
        self.event_bus.subscribe(SignalEvent, self._on_signal)
    
    async def _on_signal(self, event: SignalEvent) -> None:
        """处理交易信号"""
        if len(self.positions) >= 3:  # 最大持仓限制
            return
        
        # 模拟期权选择和执行
        await self._execute_entry(event)
    
    async def _execute_entry(self, signal: SignalEvent) -> None:
        """执行入场"""
        # 模拟期权价格
        spx_price = signal.spx_price or self.current_bar['close']
        
        # 简化：假设期权价格为 SPX 的 0.1%
        option_price = spx_price * 0.001 * (1 + np.random.uniform(-0.1, 0.1))
        
        # 模拟买入
        fill = self.broker.submit_order(
            symbol=f"SPXW_{signal.signal_type}",
            action="BUY",
            quantity=self.trading_config.execution.position_size,
            limit_price=option_price,
            current_bid=option_price * 0.98,
            current_ask=option_price * 1.02
        )
        
        if fill:
            # 创建持仓
            position_id = f"pos_{len(self.trades) + 1}"
            
            position = BacktestPosition(
                id=position_id,
                symbol=fill["symbol"],
                direction=signal.signal_type,
                quantity=fill["quantity"],
                entry_price=fill["fill_price"],
                entry_time=self.current_time,
                current_price=fill["fill_price"],
                highest_price=fill["fill_price"]
            )
            
            self.positions[position_id] = position
            
            # 更新资金
            cost = fill["fill_price"] * fill["quantity"] * 100 + fill["commission"]
            self.capital -= cost
            
            logger.debug(
                f"Entry: {position.symbol} @ ${fill['fill_price']:.2f}"
            )
    
    def _check_stops(self, current_price: float) -> None:
        """检查止损条件"""
        for position_id, position in list(self.positions.items()):
            # 更新价格
            position.current_price = current_price
            
            if current_price > position.highest_price:
                position.highest_price = current_price
            
            # 计算盈亏
            pnl_pct = (current_price - position.entry_price) / position.entry_price
            
            # 硬止损
            if pnl_pct <= -self.trading_config.risk.hard_stop_pct:
                self._execute_exit(position, "HARD_STOP")
                continue
            
            # 保本止损激活
            if not position.breakeven_active:
                if pnl_pct >= self.trading_config.risk.breakeven_trigger_pct:
                    position.breakeven_active = True
            
            # 保本止损触发
            if position.breakeven_active:
                if current_price <= position.entry_price * 1.01:
                    self._execute_exit(position, "BREAKEVEN")
                    continue
            
            # 追踪止损激活
            if not position.trailing_active:
                if pnl_pct >= self.trading_config.risk.trailing_activation_pct:
                    position.trailing_active = True
            
            # 追踪止损触发
            if position.trailing_active:
                trailing_stop = position.highest_price * (1 - self.trading_config.risk.trailing_stop_pct)
                if current_price <= trailing_stop:
                    self._execute_exit(position, "TRAILING_STOP")
    
    def _execute_exit(self, position: BacktestPosition, stop_type: str) -> None:
        """执行平仓"""
        # 模拟追单止损
        bid_prices = [
            position.current_price * (1 - 0.02 * i)
            for i in range(5)
        ]
        
        stop_result = self.broker.simulate_chase_stop(
            position.entry_price,
            position.current_price,
            bid_prices
        )
        
        if stop_result["fill_price"] is None:
            # 放弃止损
            logger.debug(f"Stop abandoned for {position.symbol}")
            return
        
        fill_price = stop_result["fill_price"]
        commission = self.broker.commission * position.quantity
        
        # 计算盈亏
        pnl = (fill_price - position.entry_price) * position.quantity * 100 - commission
        pnl_pct = (fill_price - position.entry_price) / position.entry_price
        
        # 记录交易
        trade = BacktestTrade(
            trade_id=f"trade_{len(self.trades) + 1}",
            symbol=position.symbol,
            direction=position.direction,
            entry_time=position.entry_time,
            exit_time=self.current_time,
            entry_price=position.entry_price,
            exit_price=fill_price,
            quantity=position.quantity,
            pnl=pnl,
            pnl_pct=pnl_pct,
            stop_type=stop_type,
            commission=commission
        )
        
        self.trades.append(trade)
        
        # 更新资金
        proceeds = fill_price * position.quantity * 100 - commission
        self.capital += proceeds
        
        # 移除持仓
        del self.positions[position.id]
        
        logger.debug(
            f"Exit: {position.symbol} @ ${fill_price:.2f} | "
            f"PnL=${pnl:.2f} ({pnl_pct:.1%}) | {stop_type}"
        )
    
    # ========================================================================
    # 回测运行
    # ========================================================================
    
    def load_data(
        self,
        bar_data_path: str,
        option_data_path: Optional[str] = None
    ) -> None:
        """加载回测数据"""
        # 加载 K 线数据
        path = Path(bar_data_path)
        
        if path.suffix == ".parquet":
            self.bar_data = pd.read_parquet(path)
        else:
            self.bar_data = pd.read_csv(path, parse_dates=['timestamp'])
            self.bar_data.set_index('timestamp', inplace=True)
        
        logger.info(f"Loaded {len(self.bar_data)} bars from {bar_data_path}")
        
        # 加载期权数据（可选）
        if option_data_path:
            # TODO: 加载期权历史数据
            pass
    
    async def run(
        self,
        start_date: str = None,
        end_date: str = None,
        progress_callback: Optional[Callable[[float], None]] = None
    ) -> BacktestResult:
        """
        运行回测
        
        Args:
            start_date: 开始日期
            end_date: 结束日期
            progress_callback: 进度回调
        
        Returns:
            BacktestResult
        """
        logger.info(f"Starting backtest: {start_date} to {end_date}")
        
        # 过滤日期范围
        data = self.bar_data.copy()
        
        if start_date:
            data = data[data.index >= start_date]
        if end_date:
            data = data[data.index <= end_date]
        
        if data.empty:
            logger.error("No data in specified date range")
            return self._generate_result()
        
        # 初始化策略
        from core.state import TradingState
        
        # 使用内存数据库
        state = TradingState(":memory:")
        await state.initialize()
        
        self.strategy = ChannelBreakoutStrategy(
            self.trading_config.strategy,
            self.event_bus,
            state
        )
        
        # 遍历数据
        total_bars = len(data)
        
        for i, (timestamp, row) in enumerate(data.iterrows()):
            self.current_time = timestamp
            self.current_bar = row
            
            # 创建 K 线事件
            bar_event = BarEvent(
                symbol="SPX",
                timeframe=self.trading_config.strategy.bar_size,
                bar_time=timestamp,
                open=row['open'],
                high=row['high'],
                low=row['low'],
                close=row['close'],
                volume=row.get('volume', 0),
                is_complete=True
            )
            
            # 发布事件，触发策略
            await self.event_bus.publish(bar_event)
            
            # 模拟期权价格变动，检查止损
            option_price = row['close'] * 0.001 * (1 + np.random.uniform(-0.05, 0.05))
            self._check_stops(option_price)
            
            # 更新资金曲线
            unrealized = sum(
                (p.current_price - p.entry_price) * p.quantity * 100
                for p in self.positions.values()
            )
            self.equity_curve.append(self.capital + unrealized)
            
            # 进度回调
            if progress_callback and i % 100 == 0:
                progress_callback((i + 1) / total_bars)
        
        # 平仓剩余持仓
        for position in list(self.positions.values()):
            self._execute_exit(position, "EOD")
        
        # 关闭状态
        await state.close()
        
        logger.info(f"Backtest complete: {len(self.trades)} trades")
        
        return self._generate_result()
    
    def _generate_result(self) -> BacktestResult:
        """生成回测结果"""
        if not self.trades:
            return BacktestResult(
                start_date="",
                end_date="",
                initial_capital=self.initial_capital,
                final_capital=self.capital,
                total_return=0,
                total_return_pct=0,
                total_trades=0,
                winning_trades=0,
                losing_trades=0,
                win_rate=0,
                profit_factor=0,
                max_drawdown=0,
                max_drawdown_pct=0,
                sharpe_ratio=0,
                avg_trade_pnl=0,
                avg_holding_time=0
            )
        
        # 计算统计
        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl < 0]
        
        total_pnl = sum(t.pnl for t in self.trades)
        gross_profit = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        
        # 最大回撤
        equity = np.array(self.equity_curve)
        peak = np.maximum.accumulate(equity)
        drawdown = peak - equity
        max_dd = np.max(drawdown)
        max_dd_pct = max_dd / peak[np.argmax(drawdown)] if peak[np.argmax(drawdown)] > 0 else 0
        
        # 夏普比率
        returns = np.diff(equity) / equity[:-1]
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252) if np.std(returns) > 0 else 0
        
        # 平均持仓时间
        holding_times = [
            (t.exit_time - t.entry_time).total_seconds()
            for t in self.trades
        ]
        
        return BacktestResult(
            start_date=self.trades[0].entry_time.strftime("%Y-%m-%d"),
            end_date=self.trades[-1].exit_time.strftime("%Y-%m-%d"),
            initial_capital=self.initial_capital,
            final_capital=self.capital,
            total_return=total_pnl,
            total_return_pct=total_pnl / self.initial_capital,
            total_trades=len(self.trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(self.trades),
            profit_factor=gross_profit / gross_loss if gross_loss > 0 else float('inf'),
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            sharpe_ratio=sharpe,
            avg_trade_pnl=total_pnl / len(self.trades),
            avg_holding_time=np.mean(holding_times),
            trades=self.trades,
            equity_curve=self.equity_curve
        )
    
    def get_trades_df(self) -> pd.DataFrame:
        """获取交易记录 DataFrame"""
        return pd.DataFrame([
            {
                'trade_id': t.trade_id,
                'symbol': t.symbol,
                'direction': t.direction,
                'entry_time': t.entry_time,
                'exit_time': t.exit_time,
                'entry_price': t.entry_price,
                'exit_price': t.exit_price,
                'quantity': t.quantity,
                'pnl': t.pnl,
                'pnl_pct': t.pnl_pct,
                'stop_type': t.stop_type
            }
            for t in self.trades
        ])
