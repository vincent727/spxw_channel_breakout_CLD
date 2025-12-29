"""
Channel Breakout 主策略

SPXW 0DTE 期权自动交易系统 V4

策略逻辑:
1. 计算价格通道（N 根 K 线的实体高低点）
2. 判断趋势方向（EMA 斜率）
3. 当价格突破通道且趋势一致时，产生信号
4. 多头趋势 + 向上突破 -> 买入 Call
5. 空头趋势 + 向下突破 -> 买入 Put
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, List

import pandas as pd

from core.event_bus import EventBus, EventPriority
from core.events import (
    BarEvent, SignalEvent, SPXPriceEvent, FillEvent,
    TickEvent, OrderStatusEvent
)
from core.state import TradingState, Position
from core.config import StrategyConfig

from .indicators import IndicatorEngine, ChannelResult, TrendResult

logger = logging.getLogger(__name__)


@dataclass
class StrategyState:
    """策略状态"""
    # 数据状态
    bars_loaded: bool = False
    last_bar_time: Optional[datetime] = None
    
    # 通道状态
    channel: Optional[ChannelResult] = None
    
    # 趋势状态
    trend: Optional[TrendResult] = None
    
    # 信号状态
    last_signal_time: Optional[datetime] = None
    last_signal_direction: str = ""
    cooldown_until: Optional[datetime] = None
    
    # 当前 SPX 价格
    spx_price: float = 0.0


class ChannelBreakoutStrategy:
    """
    Channel Breakout 策略
    
    核心逻辑:
    1. 每根 K 线更新时计算通道和趋势
    2. 当价格突破通道且趋势一致时产生信号
    3. 信号冷却期防止过度交易
    4. 与 OrderManager、StopManager 配合完成完整交易流程
    """
    
    def __init__(
        self,
        config: StrategyConfig,
        event_bus: EventBus,
        state: TradingState
    ):
        self.config = config
        self.event_bus = event_bus
        self.trading_state = state
        
        # 指标引擎
        self.indicators = IndicatorEngine(
            channel_lookback=config.channel_lookback,
            channel_type=config.channel_type,
            ema_period=config.ema_period,
            slope_bull_threshold=config.slope_bull_threshold,
            slope_bear_threshold=config.slope_bear_threshold
        )
        
        # 策略状态
        self.state = StrategyState()
        
        # K 线数据缓存
        self.bars: pd.DataFrame = pd.DataFrame()
        self.trend_bars: pd.DataFrame = pd.DataFrame()  # 大周期 K 线
        
        # 订阅事件
        self.event_bus.subscribe(BarEvent, self.on_bar, priority=EventPriority.NORMAL)
        self.event_bus.subscribe(SPXPriceEvent, self.on_spx_price, priority=EventPriority.NORMAL)
        self.event_bus.subscribe(FillEvent, self.on_fill, priority=EventPriority.HIGH)
        
        logger.info(
            f"ChannelBreakoutStrategy initialized: "
            f"channel_lookback={config.channel_lookback}, "
            f"channel_type={config.channel_type}, "
            f"ema_period={config.ema_period}"
        )
    
    # ========================================================================
    # K 线处理
    # ========================================================================
    
    async def on_bar(self, event: BarEvent) -> None:
        """
        K 线更新回调
        
        这是策略的主要入口
        """
        if not event.is_complete:
            return
        
        # 根据时间周期分别处理
        if event.timeframe == self.config.bar_size:
            await self._process_signal_bar(event)
        elif event.timeframe == self.config.trend_bar_size:
            await self._process_trend_bar(event)
    
    async def _process_signal_bar(self, event: BarEvent) -> None:
        """处理信号周期 K 线"""
        # 添加到缓存
        new_bar = pd.DataFrame([{
            'timestamp': event.bar_time,
            'open': event.open,
            'high': event.high,
            'low': event.low,
            'close': event.close,
            'volume': event.volume
        }])
        
        if self.bars.empty:
            self.bars = new_bar
        else:
            self.bars = pd.concat([self.bars, new_bar], ignore_index=True)
        
        # 限制缓存大小
        if len(self.bars) > 500:
            self.bars = self.bars.tail(500).reset_index(drop=True)
        
        self.state.last_bar_time = event.bar_time
        self.state.bars_loaded = True
        
        # 计算指标
        await self._calculate_indicators()
        
        # 检查信号
        await self._check_signals(event.close)
    
    async def _process_trend_bar(self, event: BarEvent) -> None:
        """处理趋势周期 K 线"""
        new_bar = pd.DataFrame([{
            'timestamp': event.bar_time,
            'open': event.open,
            'high': event.high,
            'low': event.low,
            'close': event.close,
            'volume': event.volume
        }])
        
        if self.trend_bars.empty:
            self.trend_bars = new_bar
        else:
            self.trend_bars = pd.concat([self.trend_bars, new_bar], ignore_index=True)
        
        if len(self.trend_bars) > 100:
            self.trend_bars = self.trend_bars.tail(100).reset_index(drop=True)
        
        # 更新趋势
        self._update_trend()
    
    # ========================================================================
    # 指标计算
    # ========================================================================
    
    async def _calculate_indicators(self) -> None:
        """计算指标"""
        if len(self.bars) < self.config.channel_lookback:
            return
        
        # 计算通道
        self.state.channel = self.indicators.calculate_channel(self.bars)
        
        # 计算趋势（使用信号周期，也可以用趋势周期）
        self.state.trend = self.indicators.calculate_trend(self.bars)
        
        logger.debug(
            f"Indicators updated: "
            f"Channel=[{self.state.channel.lower:.2f}, {self.state.channel.upper:.2f}] "
            f"Trend={self.state.trend.direction if self.state.trend else 'N/A'}"
        )
    
    def _update_trend(self) -> None:
        """使用大周期更新趋势"""
        if len(self.trend_bars) < self.config.ema_period:
            return
        
        # 可以在这里用大周期趋势覆盖小周期
        trend = self.indicators.calculate_trend(self.trend_bars)
        if trend:
            # 只有大周期趋势明确时才覆盖
            if trend.direction in ["BULL", "BEAR"]:
                self.state.trend = trend
    
    # ========================================================================
    # 信号检测
    # ========================================================================
    
    async def _check_signals(self, current_price: float) -> None:
        """检查交易信号"""
        # 检查前置条件
        if not self._can_generate_signal():
            return
        
        channel = self.state.channel
        trend = self.state.trend
        
        if not channel or not trend:
            return
        
        # 检查突破
        is_breakout, direction = self.indicators.check_breakout(current_price, channel)
        
        if not is_breakout:
            return
        
        # 趋势过滤
        signal = self._evaluate_signal(direction, trend)
        
        if signal:
            await self._emit_signal(signal, current_price, channel, trend)
    
    def _can_generate_signal(self) -> bool:
        """检查是否可以产生信号"""
        now = datetime.now()
        
        # 检查冷却期
        if self.state.cooldown_until and now < self.state.cooldown_until:
            return False
        
        # 检查信号锁定 (使用配置)
        if self.state.last_signal_time:
            elapsed = (now - self.state.last_signal_time).total_seconds()
            if elapsed < self.config.signal_lock_seconds:
                return False
        
        # 检查当前持仓 (使用配置中的 max_concurrent_positions)
        positions = self.trading_state.get_all_positions()
        max_positions = getattr(self.config, 'max_concurrent_positions', 3)
        if len(positions) >= max_positions:
            return False
        
        return True
    
    def _evaluate_signal(
        self,
        breakout_direction: str,
        trend: TrendResult
    ) -> Optional[str]:
        """
        评估信号
        
        趋势确认:
        - 向上突破 + 多头趋势 -> LONG_CALL
        - 向下突破 + 空头趋势 -> LONG_PUT
        """
        if breakout_direction == "UP":
            if trend.direction == "BULL":
                return "LONG_CALL"
            elif trend.direction == "NEUTRAL" and trend.slope > 0:
                # 中性但偏多
                return "LONG_CALL"
        
        elif breakout_direction == "DOWN":
            if trend.direction == "BEAR":
                return "LONG_PUT"
            elif trend.direction == "NEUTRAL" and trend.slope < 0:
                # 中性但偏空
                return "LONG_PUT"
        
        # 趋势不确认，不产生信号
        logger.debug(
            f"Signal filtered: breakout={breakout_direction}, "
            f"trend={trend.direction}"
        )
        return None
    
    async def _emit_signal(
        self,
        signal_type: str,
        price: float,
        channel: ChannelResult,
        trend: TrendResult
    ) -> None:
        """发布交易信号"""
        logger.info(
            f"📈 SIGNAL: {signal_type} | "
            f"Price=${price:.2f} | "
            f"Channel=[{channel.lower:.2f}, {channel.upper:.2f}] | "
            f"Trend={trend.direction} (slope={trend.slope:.4f})"
        )
        
        # 更新状态
        self.state.last_signal_time = datetime.now()
        self.state.last_signal_direction = signal_type
        
        # 设置冷却期
        self.state.cooldown_until = datetime.now() + timedelta(
            seconds=self.config.cooldown_bars * 300  # 假设 5 分钟 K 线
        )
        
        # 发布信号事件
        await self.event_bus.publish(SignalEvent(
            signal_type=signal_type,
            direction="BUY",
            strength=trend.strength,
            reason=f"Channel breakout with {trend.direction} trend",
            channel_upper=channel.upper,
            channel_lower=channel.lower,
            spx_price=price,
            regime=trend.direction
        ))
    
    # ========================================================================
    # 事件处理
    # ========================================================================
    
    async def on_spx_price(self, event: SPXPriceEvent) -> None:
        """SPX 价格更新"""
        self.state.spx_price = event.price
    
    async def on_fill(self, event: FillEvent) -> None:
        """成交回调"""
        if event.is_entry:
            logger.info(
                f"Entry filled: {event.contract_symbol} @ ${event.fill_price:.2f}"
            )
        else:
            logger.info(
                f"Exit filled: {event.contract_symbol} @ ${event.fill_price:.2f}"
            )
    
    # ========================================================================
    # 数据加载
    # ========================================================================
    
    def load_historical_bars(self, bars: pd.DataFrame) -> None:
        """加载历史 K 线数据"""
        self.bars = bars.copy()
        self.state.bars_loaded = True
        
        # 初始化指标
        if len(self.bars) >= self.config.channel_lookback:
            self.state.channel = self.indicators.calculate_channel(self.bars)
            self.state.trend = self.indicators.calculate_trend(self.bars)
        
        logger.info(f"Loaded {len(bars)} historical bars")
    
    # ========================================================================
    # 状态查询
    # ========================================================================
    
    def get_status(self) -> dict:
        """获取策略状态"""
        return {
            "bars_loaded": self.state.bars_loaded,
            "bar_count": len(self.bars),
            "last_bar_time": self.state.last_bar_time.isoformat() if self.state.last_bar_time else None,
            "channel": {
                "upper": self.state.channel.upper if self.state.channel else None,
                "lower": self.state.channel.lower if self.state.channel else None,
                "width_pct": self.state.channel.width_pct if self.state.channel else None
            },
            "trend": {
                "direction": self.state.trend.direction if self.state.trend else None,
                "slope": self.state.trend.slope if self.state.trend else None,
                "ema": self.state.trend.ema if self.state.trend else None
            },
            "last_signal": {
                "time": self.state.last_signal_time.isoformat() if self.state.last_signal_time else None,
                "direction": self.state.last_signal_direction
            },
            "spx_price": self.state.spx_price
        }
    
    def reset(self) -> None:
        """重置策略状态"""
        self.state = StrategyState()
        self.bars = pd.DataFrame()
        self.trend_bars = pd.DataFrame()
        self.indicators.reset()
        logger.info("Strategy reset")
