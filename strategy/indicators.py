"""
技术指标模块 - 主循环轻量计算

SPXW 0DTE 期权自动交易系统 V4

设计原则:
- 所有计算都足够轻量，可在主循环内完成（微秒级）
- 使用增量计算避免重复计算历史
- 不使用 ProcessPool（通信开销 > 计算本身）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ChannelResult:
    """通道计算结果"""
    upper: float
    lower: float
    middle: float
    width: float
    width_pct: float


@dataclass
class TrendResult:
    """趋势计算结果"""
    ema: float
    slope: float
    direction: str  # "BULL", "BEAR", "NEUTRAL"
    strength: float  # 0-1


@dataclass
class IndicatorState:
    """指标状态（用于增量计算）"""
    # EMA 状态
    ema_value: float = 0.0
    ema_initialized: bool = False
    
    # 通道状态
    channel_upper: float = 0.0
    channel_lower: float = 0.0
    
    # 趋势状态
    prev_ema: float = 0.0
    slope: float = 0.0


class IndicatorEngine:
    """
    指标计算引擎
    
    所有计算都在主循环内执行（微秒级）
    
    支持:
    - 通道突破（Body/Wick）
    - EMA + 斜率
    - 趋势判断
    """
    
    def __init__(
        self,
        channel_lookback: int = 20,
        channel_type: str = "body",
        ema_period: int = 20,
        slope_bull_threshold: float = 0.0005,
        slope_bear_threshold: float = -0.0005
    ):
        self.channel_lookback = channel_lookback
        self.channel_type = channel_type
        self.ema_period = ema_period
        self.slope_bull_threshold = slope_bull_threshold
        self.slope_bear_threshold = slope_bear_threshold
        
        # 状态
        self.state = IndicatorState()
        
        # EMA 乘数
        self.ema_multiplier = 2 / (ema_period + 1)
    
    # ========================================================================
    # 通道计算
    # ========================================================================
    
    def calculate_channel(self, bars: pd.DataFrame) -> Optional[ChannelResult]:
        """
        计算通道（微秒级）
        
        Args:
            bars: K线数据，需包含 open, high, low, close
        
        Returns:
            ChannelResult 或 None
        """
        if len(bars) < self.channel_lookback:
            return None
        
        # 取最近 N 根 K 线
        recent = bars.tail(self.channel_lookback)
        
        if self.channel_type == "body":
            # 实体通道
            body_high = recent[['open', 'close']].max(axis=1)
            body_low = recent[['open', 'close']].min(axis=1)
            upper = body_high.max()
            lower = body_low.min()
        else:
            # 影线通道
            upper = recent['high'].max()
            lower = recent['low'].min()
        
        middle = (upper + lower) / 2
        width = upper - lower
        width_pct = width / middle if middle > 0 else 0
        
        # 更新状态
        self.state.channel_upper = upper
        self.state.channel_lower = lower
        
        return ChannelResult(
            upper=upper,
            lower=lower,
            middle=middle,
            width=width,
            width_pct=width_pct
        )
    
    def calculate_channel_numpy(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray
    ) -> Optional[ChannelResult]:
        """
        使用 numpy 计算通道（更快）
        """
        if len(closes) < self.channel_lookback:
            return None
        
        n = self.channel_lookback
        
        if self.channel_type == "body":
            body_high = np.maximum(opens[-n:], closes[-n:])
            body_low = np.minimum(opens[-n:], closes[-n:])
            upper = np.max(body_high)
            lower = np.min(body_low)
        else:
            upper = np.max(highs[-n:])
            lower = np.min(lows[-n:])
        
        middle = (upper + lower) / 2
        width = upper - lower
        width_pct = width / middle if middle > 0 else 0
        
        self.state.channel_upper = upper
        self.state.channel_lower = lower
        
        return ChannelResult(
            upper=upper,
            lower=lower,
            middle=middle,
            width=width,
            width_pct=width_pct
        )
    
    # ========================================================================
    # EMA 计算
    # ========================================================================
    
    def update_ema_incremental(self, new_close: float) -> float:
        """
        EMA 增量更新（纳秒级）
        
        只需要新的收盘价，不需要完整历史
        """
        if not self.state.ema_initialized:
            self.state.ema_value = new_close
            self.state.ema_initialized = True
        else:
            self.state.ema_value = (
                (new_close - self.state.ema_value) * self.ema_multiplier 
                + self.state.ema_value
            )
        
        return self.state.ema_value
    
    def calculate_ema_full(self, closes: np.ndarray) -> float:
        """
        计算完整 EMA（用于初始化）
        """
        if len(closes) < self.ema_period:
            return closes[-1] if len(closes) > 0 else 0
        
        # 使用 pandas 的 ewm 或手动计算
        ema = closes[0]
        for close in closes[1:]:
            ema = (close - ema) * self.ema_multiplier + ema
        
        self.state.ema_value = ema
        self.state.ema_initialized = True
        
        return ema
    
    # ========================================================================
    # 趋势判断
    # ========================================================================
    
    def calculate_trend(self, bars: pd.DataFrame) -> Optional[TrendResult]:
        """
        计算趋势
        
        基于 EMA 斜率判断趋势方向
        """
        if len(bars) < self.ema_period + 1:
            return None
        
        closes = bars['close'].values
        
        # 计算当前 EMA
        current_ema = self.calculate_ema_full(closes)
        
        # 计算前一个 EMA（用于斜率）
        prev_ema = self.calculate_ema_full(closes[:-1])
        
        # 计算斜率（归一化）
        slope = (current_ema - prev_ema) / prev_ema if prev_ema > 0 else 0
        
        # 判断方向
        if slope >= self.slope_bull_threshold:
            direction = "BULL"
            strength = min(slope / self.slope_bull_threshold, 1.0)
        elif slope <= self.slope_bear_threshold:
            direction = "BEAR"
            strength = min(abs(slope) / abs(self.slope_bear_threshold), 1.0)
        else:
            direction = "NEUTRAL"
            strength = 0.0
        
        # 更新状态
        self.state.prev_ema = prev_ema
        self.state.slope = slope
        
        return TrendResult(
            ema=current_ema,
            slope=slope,
            direction=direction,
            strength=strength
        )
    
    def update_trend_incremental(self, new_close: float) -> Optional[TrendResult]:
        """
        增量更新趋势（用于 Tick 级更新）
        """
        if not self.state.ema_initialized:
            return None
        
        prev_ema = self.state.ema_value
        current_ema = self.update_ema_incremental(new_close)
        
        # 计算斜率
        slope = (current_ema - prev_ema) / prev_ema if prev_ema > 0 else 0
        
        # 判断方向
        if slope >= self.slope_bull_threshold:
            direction = "BULL"
            strength = min(slope / self.slope_bull_threshold, 1.0)
        elif slope <= self.slope_bear_threshold:
            direction = "BEAR"
            strength = min(abs(slope) / abs(self.slope_bear_threshold), 1.0)
        else:
            direction = "NEUTRAL"
            strength = 0.0
        
        self.state.slope = slope
        
        return TrendResult(
            ema=current_ema,
            slope=slope,
            direction=direction,
            strength=strength
        )
    
    # ========================================================================
    # 突破检测
    # ========================================================================
    
    def check_breakout(
        self,
        price: float,
        channel: Optional[ChannelResult] = None
    ) -> Tuple[bool, str]:
        """
        检测突破
        
        Returns:
            (is_breakout, direction): direction = "UP", "DOWN", ""
        """
        if channel is None:
            upper = self.state.channel_upper
            lower = self.state.channel_lower
        else:
            upper = channel.upper
            lower = channel.lower
        
        if upper == 0 or lower == 0:
            return False, ""
        
        if price > upper:
            return True, "UP"
        elif price < lower:
            return True, "DOWN"
        else:
            return False, ""
    
    # ========================================================================
    # 状态管理
    # ========================================================================
    
    def reset(self) -> None:
        """重置所有状态"""
        self.state = IndicatorState()
    
    def get_state(self) -> dict:
        """获取当前状态"""
        return {
            "ema": self.state.ema_value,
            "ema_initialized": self.state.ema_initialized,
            "channel_upper": self.state.channel_upper,
            "channel_lower": self.state.channel_lower,
            "slope": self.state.slope
        }


# ============================================================================
# 辅助函数
# ============================================================================

def calculate_atr(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14
) -> float:
    """计算 ATR"""
    if len(closes) < period + 1:
        return 0
    
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        tr_list.append(tr)
    
    if len(tr_list) < period:
        return np.mean(tr_list)
    
    return np.mean(tr_list[-period:])


def calculate_rsi(closes: np.ndarray, period: int = 14) -> float:
    """计算 RSI"""
    if len(closes) < period + 1:
        return 50
    
    deltas = np.diff(closes[-period-1:])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    
    avg_gain = np.mean(gains)
    avg_loss = np.mean(losses)
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def calculate_bollinger_bands(
    closes: np.ndarray,
    period: int = 20,
    std_dev: float = 2.0
) -> Tuple[float, float, float]:
    """计算布林带"""
    if len(closes) < period:
        return 0, 0, 0
    
    recent = closes[-period:]
    middle = np.mean(recent)
    std = np.std(recent)
    
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    
    return upper, middle, lower
