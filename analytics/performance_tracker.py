"""
绩效追踪器模块

SPXW 0DTE 期权自动交易系统 V4

功能:
1. 实时绩效计算
2. 历史统计
3. 风险指标
4. 报告生成
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional
import math

import pandas as pd
import numpy as np

from core.event_bus import EventBus
from core.events import TradeClosedEvent, DailySummaryEvent, FillEvent
from core.state import TradingState, Trade

logger = logging.getLogger(__name__)


@dataclass
class TradeMetrics:
    """单笔交易指标"""
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
    holding_time_seconds: float
    stop_type: str = ""
    commission: float = 0.0


@dataclass
class DailyMetrics:
    """每日指标"""
    date: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    profit_factor: float = 0.0
    avg_holding_time: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0


@dataclass
class OverallMetrics:
    """总体指标"""
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    trading_days: int = 0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    avg_pnl_per_trade: float = 0.0
    avg_pnl_per_day: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    calmar_ratio: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    avg_holding_time: float = 0.0
    consecutive_wins: int = 0
    consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0


class PerformanceTracker:
    """
    绩效追踪器
    
    功能:
    1. 实时追踪交易绩效
    2. 计算各类风险指标
    3. 生成日报/周报/月报
    """
    
    def __init__(
        self,
        event_bus: EventBus,
        state: TradingState,
        risk_free_rate: float = 0.05
    ):
        self.event_bus = event_bus
        self.state = state
        self.risk_free_rate = risk_free_rate
        
        # 交易记录缓存
        self.trades: List[TradeMetrics] = []
        
        # 每日统计缓存
        self.daily_metrics: Dict[str, DailyMetrics] = {}
        
        # 累计曲线
        self.equity_curve: List[float] = [0.0]
        self.pnl_series: List[float] = []
        
        # 当前连续胜负
        self._current_streak: int = 0  # 正数为连胜，负数为连败
        self._max_win_streak: int = 0
        self._max_loss_streak: int = 0
        
        # 订阅事件
        self.event_bus.subscribe(TradeClosedEvent, self.on_trade_closed)
        
        logger.info("PerformanceTracker initialized")
    
    # ========================================================================
    # 事件处理
    # ========================================================================
    
    async def on_trade_closed(self, event: TradeClosedEvent) -> None:
        """交易关闭事件处理"""
        metrics = TradeMetrics(
            trade_id=event.trade_id,
            symbol=event.contract_symbol,
            direction=event.direction,
            entry_time=event.entry_time,
            exit_time=event.exit_time,
            entry_price=event.entry_price,
            exit_price=event.exit_price,
            quantity=event.quantity,
            pnl=event.realized_pnl,
            pnl_pct=event.realized_pnl_pct,
            holding_time_seconds=(event.exit_time - event.entry_time).total_seconds(),
            stop_type=event.stop_type,
            commission=event.commission
        )
        
        self._record_trade(metrics)
        
        # 更新每日统计
        trade_date = event.exit_time.strftime("%Y-%m-%d")
        self._update_daily_metrics(trade_date)
        
        logger.info(
            f"Trade recorded: {metrics.symbol} | "
            f"PnL=${metrics.pnl:.2f} ({metrics.pnl_pct:.1%}) | "
            f"Holding={metrics.holding_time_seconds:.0f}s"
        )
    
    def _record_trade(self, metrics: TradeMetrics) -> None:
        """记录交易"""
        self.trades.append(metrics)
        
        # 更新累计曲线
        new_equity = self.equity_curve[-1] + metrics.pnl
        self.equity_curve.append(new_equity)
        self.pnl_series.append(metrics.pnl)
        
        # 更新连续胜负
        if metrics.pnl > 0:
            if self._current_streak > 0:
                self._current_streak += 1
            else:
                self._current_streak = 1
            self._max_win_streak = max(self._max_win_streak, self._current_streak)
        elif metrics.pnl < 0:
            if self._current_streak < 0:
                self._current_streak -= 1
            else:
                self._current_streak = -1
            self._max_loss_streak = max(self._max_loss_streak, abs(self._current_streak))
    
    def _update_daily_metrics(self, trade_date: str) -> None:
        """更新每日指标"""
        # 获取当天的交易
        day_trades = [
            t for t in self.trades
            if t.exit_time.strftime("%Y-%m-%d") == trade_date
        ]
        
        if not day_trades:
            return
        
        wins = [t for t in day_trades if t.pnl > 0]
        losses = [t for t in day_trades if t.pnl < 0]
        
        total_pnl = sum(t.pnl for t in day_trades)
        gross_profit = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        
        metrics = DailyMetrics(
            date=trade_date,
            total_trades=len(day_trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(day_trades) if day_trades else 0,
            total_pnl=total_pnl,
            avg_win=gross_profit / len(wins) if wins else 0,
            avg_loss=gross_loss / len(losses) if losses else 0,
            largest_win=max([t.pnl for t in wins], default=0),
            largest_loss=min([t.pnl for t in losses], default=0),
            profit_factor=gross_profit / gross_loss if gross_loss > 0 else float('inf'),
            avg_holding_time=sum(t.holding_time_seconds for t in day_trades) / len(day_trades)
        )
        
        self.daily_metrics[trade_date] = metrics
    
    # ========================================================================
    # 统计计算
    # ========================================================================
    
    def calculate_overall_metrics(self) -> OverallMetrics:
        """计算总体指标"""
        if not self.trades:
            return OverallMetrics()
        
        trades = self.trades
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl < 0]
        
        total_pnl = sum(t.pnl for t in trades)
        gross_profit = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        
        # 日期范围
        dates = sorted(set(t.exit_time.strftime("%Y-%m-%d") for t in trades))
        
        # 最大回撤
        max_dd, max_dd_pct = self._calculate_max_drawdown()
        
        # 夏普比率
        sharpe = self._calculate_sharpe_ratio()
        
        # Sortino 比率
        sortino = self._calculate_sortino_ratio()
        
        metrics = OverallMetrics(
            start_date=dates[0] if dates else None,
            end_date=dates[-1] if dates else None,
            trading_days=len(dates),
            total_trades=len(trades),
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=len(wins) / len(trades) if trades else 0,
            total_pnl=total_pnl,
            avg_pnl_per_trade=total_pnl / len(trades) if trades else 0,
            avg_pnl_per_day=total_pnl / len(dates) if dates else 0,
            avg_win=gross_profit / len(wins) if wins else 0,
            avg_loss=gross_loss / len(losses) if losses else 0,
            largest_win=max([t.pnl for t in wins], default=0),
            largest_loss=min([t.pnl for t in losses], default=0),
            profit_factor=gross_profit / gross_loss if gross_loss > 0 else float('inf'),
            max_drawdown=max_dd,
            max_drawdown_pct=max_dd_pct,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            avg_holding_time=sum(t.holding_time_seconds for t in trades) / len(trades),
            max_consecutive_wins=self._max_win_streak,
            max_consecutive_losses=self._max_loss_streak
        )
        
        # Calmar 比率
        if max_dd > 0 and len(dates) > 0:
            annual_return = (total_pnl / len(dates)) * 252
            metrics.calmar_ratio = annual_return / max_dd
        
        return metrics
    
    def _calculate_max_drawdown(self) -> tuple[float, float]:
        """计算最大回撤"""
        if len(self.equity_curve) < 2:
            return 0.0, 0.0
        
        equity = np.array(self.equity_curve)
        peak = np.maximum.accumulate(equity)
        drawdown = peak - equity
        
        max_dd = np.max(drawdown)
        
        # 计算百分比（相对于峰值）
        peak_at_max_dd = peak[np.argmax(drawdown)]
        max_dd_pct = max_dd / peak_at_max_dd if peak_at_max_dd > 0 else 0
        
        return max_dd, max_dd_pct
    
    def _calculate_sharpe_ratio(self) -> float:
        """计算年化夏普比率"""
        if len(self.pnl_series) < 2:
            return 0.0
        
        returns = np.array(self.pnl_series)
        
        # 假设每日交易
        daily_rf = self.risk_free_rate / 252
        
        excess_returns = returns - daily_rf
        
        if np.std(excess_returns) == 0:
            return 0.0
        
        sharpe = np.mean(excess_returns) / np.std(excess_returns)
        
        # 年化
        return sharpe * np.sqrt(252)
    
    def _calculate_sortino_ratio(self) -> float:
        """计算 Sortino 比率（只考虑下行风险）"""
        if len(self.pnl_series) < 2:
            return 0.0
        
        returns = np.array(self.pnl_series)
        daily_rf = self.risk_free_rate / 252
        
        excess_returns = returns - daily_rf
        
        # 下行偏差
        downside_returns = excess_returns[excess_returns < 0]
        if len(downside_returns) == 0:
            return float('inf')
        
        downside_std = np.std(downside_returns)
        
        if downside_std == 0:
            return float('inf')
        
        sortino = np.mean(excess_returns) / downside_std
        
        return sortino * np.sqrt(252)
    
    # ========================================================================
    # 报告生成
    # ========================================================================
    
    def generate_daily_report(self, trade_date: str = None) -> Dict[str, Any]:
        """生成日报"""
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")
        
        metrics = self.daily_metrics.get(trade_date)
        
        if not metrics:
            return {"date": trade_date, "message": "No trades"}
        
        return {
            "date": trade_date,
            "trades": metrics.total_trades,
            "wins": metrics.winning_trades,
            "losses": metrics.losing_trades,
            "win_rate": f"{metrics.win_rate:.1%}",
            "total_pnl": f"${metrics.total_pnl:.2f}",
            "avg_win": f"${metrics.avg_win:.2f}",
            "avg_loss": f"${metrics.avg_loss:.2f}",
            "largest_win": f"${metrics.largest_win:.2f}",
            "largest_loss": f"${metrics.largest_loss:.2f}",
            "profit_factor": f"{metrics.profit_factor:.2f}",
            "avg_holding_time": f"{metrics.avg_holding_time:.0f}s"
        }
    
    def generate_summary_report(self) -> Dict[str, Any]:
        """生成总结报告"""
        metrics = self.calculate_overall_metrics()
        
        return {
            "period": f"{metrics.start_date} to {metrics.end_date}",
            "trading_days": metrics.trading_days,
            "total_trades": metrics.total_trades,
            "win_rate": f"{metrics.win_rate:.1%}",
            "total_pnl": f"${metrics.total_pnl:.2f}",
            "avg_pnl_per_trade": f"${metrics.avg_pnl_per_trade:.2f}",
            "avg_pnl_per_day": f"${metrics.avg_pnl_per_day:.2f}",
            "profit_factor": f"{metrics.profit_factor:.2f}",
            "max_drawdown": f"${metrics.max_drawdown:.2f}",
            "sharpe_ratio": f"{metrics.sharpe_ratio:.2f}",
            "sortino_ratio": f"{metrics.sortino_ratio:.2f}",
            "max_consecutive_wins": metrics.max_consecutive_wins,
            "max_consecutive_losses": metrics.max_consecutive_losses
        }
    
    def get_equity_curve_df(self) -> pd.DataFrame:
        """获取累计曲线 DataFrame"""
        if not self.trades:
            return pd.DataFrame()
        
        data = []
        equity = 0.0
        
        for trade in self.trades:
            equity += trade.pnl
            data.append({
                'timestamp': trade.exit_time,
                'trade_id': trade.trade_id,
                'pnl': trade.pnl,
                'cumulative_pnl': equity
            })
        
        return pd.DataFrame(data).set_index('timestamp')
    
    def get_trade_history_df(self) -> pd.DataFrame:
        """获取交易历史 DataFrame"""
        if not self.trades:
            return pd.DataFrame()
        
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
                'holding_seconds': t.holding_time_seconds,
                'stop_type': t.stop_type
            }
            for t in self.trades
        ])
    
    # ========================================================================
    # 加载历史数据
    # ========================================================================
    
    async def load_from_state(self, start_date: str = None, end_date: str = None) -> None:
        """从状态管理器加载历史交易"""
        trades = await self.state.get_trades_by_date_range(start_date, end_date)
        
        for trade in trades:
            metrics = TradeMetrics(
                trade_id=trade.position_id,
                symbol=trade.contract_symbol,
                direction=trade.direction,
                entry_time=trade.entry_time,
                exit_time=trade.exit_time,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                quantity=trade.quantity,
                pnl=trade.realized_pnl,
                pnl_pct=trade.realized_pnl_pct,
                holding_time_seconds=(trade.exit_time - trade.entry_time).total_seconds(),
                stop_type=trade.stop_type or "",
                commission=trade.commission
            )
            self._record_trade(metrics)
        
        # 更新每日统计
        for trade in trades:
            trade_date = trade.exit_time.strftime("%Y-%m-%d")
            self._update_daily_metrics(trade_date)
        
        logger.info(f"Loaded {len(trades)} trades from state")
