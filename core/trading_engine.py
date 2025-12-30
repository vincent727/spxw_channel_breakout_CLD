"""
Trading Engine - 核心交易引擎

SPXW 0DTE 期权自动交易系统 V4

实现工作流:
Phase 1: Warm-up (预热)
  - 加载配置
  - 下载历史数据
  - 预计算通道和趋势

Phase 2: Subscribe Early (提前订阅)
  - 在开盘前订阅实时数据
  - 5秒K线流
  - Tick数据流

Phase 3: Immediate Action (即时行动)
  - 开盘后第一个Tick立即可以交易
  - 使用预计算的通道进行突破检查

Phase 4: Dynamic Update (动态更新)
  - K线完成时更新通道
  - 趋势K线完成时更新趋势
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timedelta
from enum import Enum
from typing import Optional, Dict, List, Any, Callable

import pandas as pd

from core.config import TradingConfig
from core.event_bus import EventBus, EventPriority
from core.events import (
    BarEvent, TickEvent, SignalEvent, SPXPriceEvent,
    SystemStatusEvent, ConnectionEvent
)
from core.calendar import get_trading_calendar

logger = logging.getLogger(__name__)


class EnginePhase(Enum):
    """引擎运行阶段"""
    IDLE = "IDLE"
    WARMING_UP = "WARMING_UP"
    SUBSCRIBED = "SUBSCRIBED"
    READY = "READY"          # 准备就绪，等待开盘
    TRADING = "TRADING"      # 交易中
    CLOSING = "CLOSING"      # 收盘处理
    STOPPED = "STOPPED"


@dataclass
class ChannelState:
    """通道状态 - 配置驱动"""
    upper: float = 0.0
    lower: float = 0.0
    mid: float = 0.0
    last_update: Optional[datetime] = None
    bars_used: int = 0
    
    @property
    def is_valid(self) -> bool:
        return self.upper > 0 and self.lower > 0 and self.upper > self.lower


@dataclass  
class TrendState:
    """趋势状态 - 配置驱动"""
    direction: str = "NEUTRAL"  # BULL / BEAR / NEUTRAL
    slope: float = 0.0
    ema_value: float = 0.0
    strength: float = 0.0
    last_update: Optional[datetime] = None
    
    @property
    def is_bullish(self) -> bool:
        return self.direction == "BULL"
    
    @property
    def is_bearish(self) -> bool:
        return self.direction == "BEAR"


@dataclass
class EngineState:
    """引擎状态"""
    phase: EnginePhase = EnginePhase.IDLE
    
    # 预计算的通道和趋势 (Phase 1 计算)
    current_channel: ChannelState = field(default_factory=ChannelState)
    current_trend: TrendState = field(default_factory=TrendState)
    
    # 当前价格
    spx_price: float = 0.0
    last_tick_time: Optional[datetime] = None
    
    # 信号冷却状态
    last_signal_time: Optional[datetime] = None
    last_signal_direction: str = ""
    in_breakout: bool = False  # 是否处于突破状态
    
    # 持仓方向过滤
    current_position_direction: str = ""  # "CALL" / "PUT" / "" (无持仓)
    
    # 止损/止盈后阻止本 bar 开仓
    signal_blocked_until_next_bar: bool = False
    
    # 数据缓存
    signal_bars: pd.DataFrame = field(default_factory=pd.DataFrame)  # 信号周期K线
    trend_bars: pd.DataFrame = field(default_factory=pd.DataFrame)   # 趋势周期K线
    
    # 形成中的K线 (来自5秒聚合)
    forming_signal_bar: Optional[Dict] = None
    forming_trend_bar: Optional[Dict] = None
    
    # 统计
    bars_received: int = 0
    ticks_received: int = 0
    signals_generated: int = 0


class TradingEngine:
    """
    交易引擎 - 实现完整的 Warm-up 工作流
    
    关键原则:
    1. ZERO HARDCODING - 所有参数来自配置
    2. 提前订阅 - 在开盘前就准备好
    3. 即时交易 - 第一个Tick就可以使用预计算值交易
    4. 动态更新 - K线完成时立即更新状态
    """
    
    def __init__(
        self,
        config: TradingConfig,
        event_bus: EventBus,
        ib_adapter: Any  # IBAdapter
    ):
        self.config = config
        self.event_bus = event_bus
        self.ib_adapter = ib_adapter
        
        # 引擎状态
        self.state = EngineState()
        
        # 配置驱动的参数 (从 config 读取)
        self._channel_lookback = config.strategy.channel_lookback
        self._channel_type = config.strategy.channel_type
        self._ema_period = config.strategy.ema_period
        self._slope_bull_threshold = config.strategy.slope_bull_threshold
        self._slope_bear_threshold = config.strategy.slope_bear_threshold
        self._signal_bar_size = config.strategy.bar_size
        self._trend_bar_size = config.strategy.trend_bar_size
        self._warmup_bars = config.strategy.warmup_bars
        self._warmup_trend_bars = config.strategy.warmup_trend_bars
        self._max_positions = config.strategy.max_concurrent_positions
        self._cooldown_bars = config.strategy.cooldown_bars
        self._signal_lock_seconds = config.strategy.signal_lock_seconds
        
        # 信号回调
        self._signal_callbacks: List[Callable] = []
        
        # SPX 合约
        self._spx_contract = None
        
        # 订阅状态
        self._realtime_bars_subscribed = False
        self._tick_subscribed = False
        
        logger.info(
            f"TradingEngine initialized: "
            f"channel_lookback={self._channel_lookback}, "
            f"ema_period={self._ema_period}, "
            f"signal_bar={self._signal_bar_size}, "
            f"trend_bar={self._trend_bar_size}"
        )
    
    def register_signal_callback(self, callback: Callable) -> None:
        """注册信号回调"""
        self._signal_callbacks.append(callback)
    
    # ========================================================================
    # Phase 1: Warm-up (预热)
    # ========================================================================
    
    async def warmup(self) -> bool:
        """
        Phase 1: 预热阶段
        
        1. 连接到 IBKR
        2. 下载历史数据
        3. 预计算通道和趋势
        
        Returns:
            是否预热成功
        """
        logger.info("=" * 60)
        logger.info("Phase 1: WARM-UP - Starting")
        logger.info("=" * 60)
        
        self.state.phase = EnginePhase.WARMING_UP
        
        # Step A: 连接
        if not await self.ib_adapter.connect():
            logger.error("Failed to connect to IBKR")
            return False
        
        # 解析 SPX 合约
        self._spx_contract = await self.ib_adapter.resolve_spx_contract()
        if not self._spx_contract:
            logger.error("Failed to resolve SPX contract")
            return False
        
        # Step B: 下载历史数据
        logger.info(f"Downloading historical data: {self._warmup_bars} signal bars, {self._warmup_trend_bars} trend bars")
        
        # 信号周期历史数据
        signal_bars = await self._download_historical_bars(
            self._signal_bar_size,
            self._warmup_bars
        )
        
        if signal_bars is None or len(signal_bars) < self._channel_lookback:
            logger.error(f"Insufficient signal bars: got {len(signal_bars) if signal_bars is not None else 0}, need {self._channel_lookback}")
            return False
        
        self.state.signal_bars = signal_bars
        logger.info(f"Loaded {len(signal_bars)} signal bars ({self._signal_bar_size})")
        
        # 趋势周期历史数据
        trend_bars = await self._download_historical_bars(
            self._trend_bar_size,
            self._warmup_trend_bars
        )
        
        if trend_bars is None or len(trend_bars) < self._ema_period:
            logger.warning(f"Insufficient trend bars: got {len(trend_bars) if trend_bars is not None else 0}, need {self._ema_period}")
            # 趋势数据不足时使用信号数据
            self.state.trend_bars = signal_bars
        else:
            self.state.trend_bars = trend_bars
            logger.info(f"Loaded {len(trend_bars)} trend bars ({self._trend_bar_size})")
        
        # Step C: 预计算通道和趋势
        self._calculate_initial_channel()
        self._calculate_initial_trend()
        
        if not self.state.current_channel.is_valid:
            logger.error("Failed to calculate initial channel")
            return False
        
        logger.info(
            f"Initial Channel: [{self.state.current_channel.lower:.2f}, "
            f"{self.state.current_channel.upper:.2f}] "
            f"(using {self.state.current_channel.bars_used} bars)"
        )
        logger.info(
            f"Initial Trend: {self.state.current_trend.direction} "
            f"(slope={self.state.current_trend.slope:.6f})"
        )
        
        logger.info("Phase 1: WARM-UP - Complete ✓")
        return True
    
    async def _download_historical_bars(
        self,
        bar_size: str,
        count: int
    ) -> Optional[pd.DataFrame]:
        """下载历史K线数据"""
        # 计算需要的时间跨度
        bar_seconds = self._parse_bar_size_to_seconds(bar_size)
        duration_seconds = bar_seconds * count * 2  # 多请求一些以确保足够
        
        # IBKR 格式的 duration
        if duration_seconds >= 86400:
            duration = f"{duration_seconds // 86400 + 1} D"
        else:
            duration = f"{duration_seconds} S"
        
        bars = await self.ib_adapter.get_historical_bars(
            self._spx_contract,
            duration=duration,
            bar_size=bar_size
        )
        
        if not bars:
            return None
        
        # 转换为 DataFrame
        df = pd.DataFrame([{
            'timestamp': b.date,
            'open': b.open,
            'high': b.high,
            'low': b.low,
            'close': b.close,
            'volume': b.volume
        } for b in bars])
        
        # 只保留需要的数量
        if len(df) > count:
            df = df.tail(count).reset_index(drop=True)
        
        return df
    
    def _parse_bar_size_to_seconds(self, bar_size: str) -> int:
        """解析K线周期为秒数 - 配置驱动"""
        mapping = {
            "1 min": 60, "1 mins": 60,
            "2 mins": 120,
            "3 mins": 180,
            "5 mins": 300,
            "10 mins": 600,
            "15 mins": 900,
            "30 mins": 1800,
            "1 hour": 3600,
            "2 hours": 7200,
            "4 hours": 14400,
            "1 day": 86400,
        }
        return mapping.get(bar_size, 300)
    
    def _calculate_initial_channel(self) -> None:
        """
        预计算初始通道 - 使用配置中的 lookback
        """
        df = self.state.signal_bars
        if df.empty or len(df) < self._channel_lookback:
            return
        
        # 使用最近 N 根 K 线 (N = config.channel_lookback)
        recent = df.tail(self._channel_lookback)
        
        if self._channel_type == "body":
            # 使用实体高低点
            highs = recent.apply(lambda r: max(r['open'], r['close']), axis=1)
            lows = recent.apply(lambda r: min(r['open'], r['close']), axis=1)
        else:
            # 使用影线高低点
            highs = recent['high']
            lows = recent['low']
        
        self.state.current_channel = ChannelState(
            upper=highs.max(),
            lower=lows.min(),
            mid=(highs.max() + lows.min()) / 2,
            last_update=datetime.now(),
            bars_used=self._channel_lookback
        )
    
    def _calculate_initial_trend(self) -> None:
        """
        预计算初始趋势 - 使用配置中的 ema_period
        """
        df = self.state.trend_bars if not self.state.trend_bars.empty else self.state.signal_bars
        
        if df.empty or len(df) < self._ema_period:
            return
        
        # 计算 EMA
        closes = df['close']
        ema = closes.ewm(span=self._ema_period, adjust=False).mean()
        
        # 计算斜率 (最近几根的变化率)
        if len(ema) >= 2:
            # 使用最近的 EMA 值计算斜率
            recent_ema = ema.tail(5)  # 使用最近 5 个值
            if len(recent_ema) >= 2:
                slope = (recent_ema.iloc[-1] - recent_ema.iloc[0]) / recent_ema.iloc[0] / len(recent_ema)
            else:
                slope = 0.0
        else:
            slope = 0.0
        
        # 判断方向 - 使用配置中的阈值
        if slope > self._slope_bull_threshold:
            direction = "BULL"
        elif slope < self._slope_bear_threshold:
            direction = "BEAR"
        else:
            direction = "NEUTRAL"
        
        # 计算强度
        max_slope = max(abs(self._slope_bull_threshold), abs(self._slope_bear_threshold))
        strength = min(abs(slope) / max_slope, 1.0) if max_slope > 0 else 0.0
        
        self.state.current_trend = TrendState(
            direction=direction,
            slope=slope,
            ema_value=ema.iloc[-1],
            strength=strength,
            last_update=datetime.now()
        )
    
    # ========================================================================
    # Phase 2: Subscribe Early (提前订阅)
    # ========================================================================
    
    async def subscribe_early(self) -> bool:
        """
        Phase 2: 提前订阅
        
        即使是 09:20 AM，也立即发送订阅请求
        这样开盘时数据就会立即到达
        """
        logger.info("=" * 60)
        logger.info("Phase 2: SUBSCRIBE EARLY - Starting")
        logger.info("=" * 60)
        
        if not self._spx_contract:
            logger.error("SPX contract not resolved")
            return False
        
        # 订阅 5 秒实时K线
        if not self._realtime_bars_subscribed:
            try:
                bars = self.ib_adapter.ib.reqRealTimeBars(
                    self._spx_contract,
                    barSize=5,
                    whatToShow='TRADES',
                    useRTH=False
                )
                bars.updateEvent += self._on_realtime_bar
                self._realtime_bars_subscribed = True
                logger.info("Subscribed to 5-second realtime bars")
            except Exception as e:
                logger.error(f"Failed to subscribe realtime bars: {e}")
                return False
        
        # 订阅 Tick 数据 (检查是否已订阅)
        if not self._tick_subscribed:
            try:
                # 检查是否已经有活跃订阅
                ticker = self.ib_adapter.active_subscriptions.get(self._spx_contract.conId)
                if not ticker:
                    ticker = await self.ib_adapter.subscribe_market_data(self._spx_contract)
                
                if ticker:
                    # 只添加一次回调
                    ticker.updateEvent += self._on_tick_update
                    self._tick_subscribed = True
                    logger.info("Subscribed to tick data")
            except Exception as e:
                logger.error(f"Failed to subscribe tick data: {e}")
        
        self.state.phase = EnginePhase.SUBSCRIBED
        
        # 发布系统状态
        await self.event_bus.publish(SystemStatusEvent(
            status="SUBSCRIBED",
            message="Early subscription complete, waiting for market open"
        ))
        
        logger.info("Phase 2: SUBSCRIBE EARLY - Complete ✓")
        return True
    
    # ========================================================================
    # Phase 3: Immediate Action (即时行动)
    # ========================================================================
    
    async def start_trading(self) -> None:
        """
        Phase 3: 开始交易
        
        此时 current_channel 和 current_trend 已经预计算好
        第一个 Tick 到达时可以立即进行突破检查
        """
        logger.info("=" * 60)
        logger.info("Phase 3: TRADING - Starting")
        logger.info("=" * 60)
        
        self.state.phase = EnginePhase.TRADING
        
        await self.event_bus.publish(SystemStatusEvent(
            status="TRADING",
            message="Trading started with pre-calculated channel and trend"
        ))
        
        logger.info(
            f"Ready to trade with: "
            f"Channel=[{self.state.current_channel.lower:.2f}, {self.state.current_channel.upper:.2f}], "
            f"Trend={self.state.current_trend.direction}"
        )
    
    def _on_tick_update(self, ticker) -> None:
        """
        Tick 更新回调 - 即时处理
        
        这是 Phase 3 的核心：使用预计算的通道进行突破检查
        """
        if self.state.phase != EnginePhase.TRADING:
            return
        
        try:
            # 获取价格
            import math
            price = ticker.last
            if price is None or (isinstance(price, float) and math.isnan(price)):
                price = ticker.close
            if price is None or (isinstance(price, float) and math.isnan(price)):
                return
            
            self.state.spx_price = price
            self.state.last_tick_time = datetime.now()
            self.state.ticks_received += 1
            
            # 使用预计算的通道检查突破
            self._check_breakout_signal(price)
            
            # 发布价格事件
            self.event_bus.publish_sync(SPXPriceEvent(
                price=price,
                timestamp=datetime.now()
            ))
            
        except Exception as e:
            logger.error(f"Error processing tick: {e}")
    
    def _check_breakout_signal(self, price: float) -> None:
        """
        检查突破信号 - 使用预计算的通道和趋势
        
        关键设计:
        1. 只在首次突破时发信号（不是每个 tick）
        2. 价格回到通道内时重置状态
        3. 信号冷却期内不发信号
        4. 已有同方向持仓时不重复开仓
        5. NEUTRAL 趋势时允许双向突破
        6. 止损/止盈后本 bar 不再开仓
        7. 开盘缓冲期内不交易
        """
        channel = self.state.current_channel
        trend = self.state.current_trend
        
        if not channel.is_valid:
            return
        
        # 检查止损/止盈后阻止标志
        if self.state.signal_blocked_until_next_bar:
            return
        
        # 检查开盘缓冲期 (0 = 无缓冲)
        buffer_minutes = self.config.risk.trading_start_buffer_minutes
        if buffer_minutes > 0:
            calendar = get_trading_calendar()
            seconds_since_open = calendar.seconds_since_market_open()
            if seconds_since_open is not None and seconds_since_open < buffer_minutes * 60:
                return
        
        # 检查信号冷却期 (使用配置中的 signal_lock_seconds)
        if self.state.last_signal_time:
            elapsed = (datetime.now() - self.state.last_signal_time).total_seconds()
            if elapsed < self._signal_lock_seconds:
                return
        
        # 判断当前位置
        is_above_upper = price > channel.upper
        is_below_lower = price < channel.lower
        is_inside_channel = channel.lower <= price <= channel.upper
        
        # 如果价格回到通道内，重置突破状态
        if is_inside_channel:
            if self.state.in_breakout:
                self.state.in_breakout = False
                self.state.last_signal_direction = ""
            return
        
        # 如果已经在突破状态且方向相同，不重复发信号
        if self.state.in_breakout:
            if (is_above_upper and self.state.last_signal_direction == "UP") or \
               (is_below_lower and self.state.last_signal_direction == "DOWN"):
                return
        
        # 检查向上突破 (首次突破)
        if is_above_upper and self.state.last_signal_direction != "UP":
            # 持仓过滤：已有 CALL 持仓时不再开 CALL
            if self.state.current_position_direction == "CALL":
                logger.debug(f"Breakout UP blocked: Already holding CALL position")
                return
            
            # 趋势条件放宽：NEUTRAL 时也允许突破
            if trend.is_bullish or trend.direction == "NEUTRAL":
                self.state.in_breakout = True
                self.state.last_signal_direction = "UP"
                self._emit_signal("LONG_CALL", price, channel, trend, "breakout_up")
        
        # 检查向下突破 (首次突破)
        elif is_below_lower and self.state.last_signal_direction != "DOWN":
            # 持仓过滤：已有 PUT 持仓时不再开 PUT
            if self.state.current_position_direction == "PUT":
                logger.debug(f"Breakout DOWN blocked: Already holding PUT position")
                return
            
            # 趋势条件放宽：NEUTRAL 时也允许突破
            if trend.is_bearish or trend.direction == "NEUTRAL":
                self.state.in_breakout = True
                self.state.last_signal_direction = "DOWN"
                self._emit_signal("LONG_PUT", price, channel, trend, "breakout_down")
    
    def _emit_signal(
        self,
        signal_type: str,
        price: float,
        channel: ChannelState,
        trend: TrendState,
        reason: str
    ) -> None:
        """发出交易信号"""
        # 更新信号时间（用于冷却机制）
        self.state.last_signal_time = datetime.now()
        
        logger.info(
            f"📈 SIGNAL: {signal_type} | "
            f"Price=${price:.2f} | "
            f"Channel=[{channel.lower:.2f}, {channel.upper:.2f}] | "
            f"Trend={trend.direction} | "
            f"Reason={reason}"
        )
        
        self.state.signals_generated += 1
        
        # 发布信号事件
        signal_event = SignalEvent(
            signal_type=signal_type,
            direction="BUY",
            strength=trend.strength,
            reason=reason,
            channel_upper=channel.upper,
            channel_lower=channel.lower,
            spx_price=price,
            regime=trend.direction
        )
        
        # 同步发布 (因为在回调中)
        self.event_bus.publish_sync(signal_event)
        
        # 调用回调 (已移除，信号通过 event_bus 传递)
        for callback in self._signal_callbacks:
            try:
                callback(signal_event)
            except Exception as e:
                logger.error(f"Signal callback error: {e}")
    
    # ========================================================================
    # Phase 4: Dynamic Update (动态更新)
    # ========================================================================
    
    def _on_realtime_bar(self, bars, hasNewBar: bool) -> None:
        """
        5秒K线回调 - 聚合并动态更新
        
        当信号周期K线完成时，更新 current_channel
        当趋势周期K线完成时，更新 current_trend
        """
        if not hasNewBar or not bars:
            return
        
        bar = bars[-1]
        self.state.bars_received += 1
        
        try:
            # 聚合到信号周期
            signal_completed = self._aggregate_to_bar(
                bar, 
                self._signal_bar_size,
                'signal'
            )
            
            # 聚合到趋势周期
            trend_completed = self._aggregate_to_bar(
                bar,
                self._trend_bar_size,
                'trend'
            )
            
            # 如果信号周期K线完成，更新通道
            if signal_completed:
                # 重置止损/止盈后的开仓阻止标志
                if self.state.signal_blocked_until_next_bar:
                    self.state.signal_blocked_until_next_bar = False
                    logger.debug("Signal block cleared (new bar started)")
                
                self._update_channel()
                channel = self.state.current_channel
                spx = self.state.spx_price
                logger.info(
                    f"📊 Channel updated: [{channel.lower:.2f}, {channel.upper:.2f}] "
                    f"| SPX=${spx:.2f} "
                    f"| {'↑ ABOVE' if spx > channel.upper else '↓ BELOW' if spx < channel.lower else '→ INSIDE'}"
                )
            
            # 如果趋势周期K线完成，更新趋势
            if trend_completed:
                self._update_trend()
                logger.info(f"📈 Trend updated: {self.state.current_trend.direction} (slope={self.state.current_trend.slope:.6f})")
                
        except Exception as e:
            logger.error(f"Error processing realtime bar: {e}")
    
    def _aggregate_to_bar(
        self,
        bar,
        target_bar_size: str,
        bar_type: str  # 'signal' or 'trend'
    ) -> bool:
        """
        将5秒K线聚合到目标周期
        
        Returns:
            是否完成了一根K线
        """
        period_seconds = self._parse_bar_size_to_seconds(target_bar_size)
        
        # 获取K线时间
        bar_time = bar.time
        if isinstance(bar_time, datetime):
            timestamp = bar_time.timestamp()
        else:
            timestamp = datetime.combine(bar_time, datetime.min.time()).timestamp()
        
        # 计算周期开始时间
        period_start_ts = (int(timestamp) // period_seconds) * period_seconds
        period_start = datetime.fromtimestamp(period_start_ts)
        
        # 获取或创建形成中的K线
        if bar_type == 'signal':
            forming = self.state.forming_signal_bar
        else:
            forming = self.state.forming_trend_bar
        
        completed = False
        
        # 检查是否需要开始新的周期
        if forming is None or forming.get('period_start') != period_start:
            # 如果有旧的K线，它已完成
            if forming is not None:
                completed = True
                self._finalize_bar(forming, bar_type)
            
            # 开始新的K线
            forming = {
                'period_start': period_start,
                'open': bar.open_,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume
            }
        else:
            # 更新形成中的K线
            forming['high'] = max(forming['high'], bar.high)
            forming['low'] = min(forming['low'], bar.low)
            forming['close'] = bar.close
            forming['volume'] += bar.volume
        
        # 保存
        if bar_type == 'signal':
            self.state.forming_signal_bar = forming
        else:
            self.state.forming_trend_bar = forming
        
        return completed
    
    def _finalize_bar(self, bar_data: Dict, bar_type: str) -> None:
        """完成一根K线，添加到历史数据"""
        new_bar = pd.DataFrame([{
            'timestamp': bar_data['period_start'],
            'open': bar_data['open'],
            'high': bar_data['high'],
            'low': bar_data['low'],
            'close': bar_data['close'],
            'volume': bar_data['volume']
        }])
        
        if bar_type == 'signal':
            self.state.signal_bars = pd.concat(
                [self.state.signal_bars, new_bar], 
                ignore_index=True
            )
            # 限制缓存大小 (使用配置)
            max_bars = self._warmup_bars * 2
            if len(self.state.signal_bars) > max_bars:
                self.state.signal_bars = self.state.signal_bars.tail(max_bars).reset_index(drop=True)
            
            # 发布 BarEvent
            self.event_bus.publish_sync(BarEvent(
                symbol="SPX",
                timeframe=self._signal_bar_size,
                bar_time=bar_data['period_start'],
                open=bar_data['open'],
                high=bar_data['high'],
                low=bar_data['low'],
                close=bar_data['close'],
                volume=bar_data['volume'],
                is_complete=True
            ))
        else:
            self.state.trend_bars = pd.concat(
                [self.state.trend_bars, new_bar],
                ignore_index=True
            )
            max_bars = self._warmup_trend_bars * 2
            if len(self.state.trend_bars) > max_bars:
                self.state.trend_bars = self.state.trend_bars.tail(max_bars).reset_index(drop=True)
            
            # 发布 BarEvent
            self.event_bus.publish_sync(BarEvent(
                symbol="SPX",
                timeframe=self._trend_bar_size,
                bar_time=bar_data['period_start'],
                open=bar_data['open'],
                high=bar_data['high'],
                low=bar_data['low'],
                close=bar_data['close'],
                volume=bar_data['volume'],
                is_complete=True
            ))
    
    def _update_channel(self) -> None:
        """
        动态更新通道 - 使用最新的 N 根K线
        
        N = config.strategy.channel_lookback
        """
        df = self.state.signal_bars
        if df.empty or len(df) < self._channel_lookback:
            return
        
        recent = df.tail(self._channel_lookback)
        
        if self._channel_type == "body":
            highs = recent.apply(lambda r: max(r['open'], r['close']), axis=1)
            lows = recent.apply(lambda r: min(r['open'], r['close']), axis=1)
        else:
            highs = recent['high']
            lows = recent['low']
        
        self.state.current_channel = ChannelState(
            upper=highs.max(),
            lower=lows.min(),
            mid=(highs.max() + lows.min()) / 2,
            last_update=datetime.now(),
            bars_used=self._channel_lookback
        )
    
    def _update_trend(self) -> None:
        """
        动态更新趋势 - 使用最新的 M 根K线
        
        M = config.strategy.ema_period
        """
        df = self.state.trend_bars if not self.state.trend_bars.empty else self.state.signal_bars
        
        if df.empty or len(df) < self._ema_period:
            return
        
        closes = df['close']
        ema = closes.ewm(span=self._ema_period, adjust=False).mean()
        
        if len(ema) >= 5:
            recent_ema = ema.tail(5)
            slope = (recent_ema.iloc[-1] - recent_ema.iloc[0]) / recent_ema.iloc[0] / len(recent_ema)
        else:
            slope = 0.0
        
        if slope > self._slope_bull_threshold:
            direction = "BULL"
        elif slope < self._slope_bear_threshold:
            direction = "BEAR"
        else:
            direction = "NEUTRAL"
        
        max_slope = max(abs(self._slope_bull_threshold), abs(self._slope_bear_threshold))
        strength = min(abs(slope) / max_slope, 1.0) if max_slope > 0 else 0.0
        
        self.state.current_trend = TrendState(
            direction=direction,
            slope=slope,
            ema_value=ema.iloc[-1],
            strength=strength,
            last_update=datetime.now()
        )
    
    # ========================================================================
    # 生命周期管理
    # ========================================================================
    
    async def run(self) -> None:
        """
        运行完整的交易流程
        
        Phase 1: Warm-up
        Phase 2: Subscribe Early
        Phase 3: Wait for market open, then start trading
        Phase 4: Dynamic updates continue automatically
        """
        # Phase 1: Warm-up
        if not await self.warmup():
            logger.error("Warm-up failed, cannot start")
            return
        
        # Phase 2: Subscribe Early
        if not await self.subscribe_early():
            logger.error("Early subscription failed")
            return
        
        # 检查市场状态
        calendar = get_trading_calendar()
        status = calendar.get_trading_status()
        
        logger.info(f"Market status: {status['reason']}")
        
        if status['is_market_open']:
            # 已经开盘，立即开始交易
            await self.start_trading()
        else:
            # 等待开盘
            self.state.phase = EnginePhase.READY
            logger.info("Waiting for market open...")
            
            # 等待开盘的循环
            while not calendar.is_market_open():
                await asyncio.sleep(1)
                if self.state.phase == EnginePhase.STOPPED:
                    return
            
            # 开盘了
            await self.start_trading()
        
        # 主循环
        while self.state.phase == EnginePhase.TRADING:
            await asyncio.sleep(0.1)
            
            # 检查收盘
            if not calendar.is_market_open():
                break
        
        logger.info("Trading session ended")
    
    async def stop(self) -> None:
        """停止引擎"""
        logger.info("Stopping trading engine...")
        self.state.phase = EnginePhase.STOPPED
        
        # 取消订阅
        if self._realtime_bars_subscribed and self._spx_contract:
            try:
                self.ib_adapter.ib.cancelRealTimeBars(self._spx_contract)
            except:
                pass
        
        logger.info(
            f"Engine stopped: "
            f"bars_received={self.state.bars_received}, "
            f"ticks_received={self.state.ticks_received}, "
            f"signals_generated={self.state.signals_generated}"
        )
    
    def get_status(self) -> Dict:
        """获取引擎状态"""
        return {
            "phase": self.state.phase.value,
            "channel": {
                "upper": self.state.current_channel.upper,
                "lower": self.state.current_channel.lower,
                "mid": self.state.current_channel.mid,
                "valid": self.state.current_channel.is_valid
            },
            "trend": {
                "direction": self.state.current_trend.direction,
                "slope": self.state.current_trend.slope,
                "strength": self.state.current_trend.strength
            },
            "spx_price": self.state.spx_price,
            "bars_received": self.state.bars_received,
            "ticks_received": self.state.ticks_received,
            "signals_generated": self.state.signals_generated,
            "config": {
                "channel_lookback": self._channel_lookback,
                "ema_period": self._ema_period,
                "signal_bar_size": self._signal_bar_size,
                "trend_bar_size": self._trend_bar_size
            }
        }
