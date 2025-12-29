"""
K 线聚合器 - 将 IBKR 5 秒实时 K 线聚合成不同周期

SPXW 0DTE 期权自动交易系统 V4

特性:
- 支持多周期同时聚合 (1分钟、5分钟、15分钟等)
- 自动对齐到周期边界
- 发布 BarEvent 到事件总线
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Any
from collections import defaultdict

from ib_insync import Contract, RealTimeBar

logger = logging.getLogger(__name__)


@dataclass
class BarData:
    """K 线数据"""
    symbol: str
    timeframe: str  # "1min", "5min", "15min", etc.
    timestamp: datetime  # K 线开始时间
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    is_complete: bool = False


@dataclass
class AggregatorConfig:
    """聚合器配置"""
    timeframes: List[str] = field(default_factory=lambda: ["5 mins"])
    
    # 周期对应的秒数 (支持 IBKR 格式)
    TIMEFRAME_SECONDS = {
        # 短格式
        "1min": 60,
        "2min": 120,
        "3min": 180,
        "5min": 300,
        "10min": 600,
        "15min": 900,
        "30min": 1800,
        "1hour": 3600,
        # IBKR 格式
        "1 min": 60,
        "2 mins": 120,
        "3 mins": 180,
        "5 mins": 300,
        "10 mins": 600,
        "15 mins": 900,
        "30 mins": 1800,
        "1 hour": 3600,
        # 秒
        "5 secs": 5,
        "10 secs": 10,
        "15 secs": 15,
        "30 secs": 30,
    }
    
    @classmethod
    def get_seconds(cls, timeframe: str) -> int:
        """获取周期对应的秒数"""
        return cls.TIMEFRAME_SECONDS.get(timeframe, 300)


class BarAggregator:
    """
    K 线聚合器
    
    将 IBKR 5 秒实时 K 线聚合成不同周期的 K 线
    """
    
    def __init__(
        self,
        ib_adapter: Any,
        event_bus: Any,
        timeframes: List[str] = None
    ):
        self.ib_adapter = ib_adapter
        self.event_bus = event_bus
        self.timeframes = timeframes or ["5min"]
        
        # 每个 symbol 每个周期的聚合状态
        # {symbol: {timeframe: BarData}}
        self._current_bars: Dict[str, Dict[str, BarData]] = defaultdict(dict)
        
        # 活跃的订阅
        self._subscriptions: Dict[int, Contract] = {}  # contract_id -> Contract
        
        # 运行状态
        self._running = False
        
        logger.info(f"BarAggregator initialized: timeframes={timeframes}")
    
    async def subscribe(self, contract: Contract) -> bool:
        """
        订阅实时 5 秒 K 线
        
        Args:
            contract: 要订阅的合约
            
        Returns:
            是否订阅成功
        """
        if contract.conId in self._subscriptions:
            logger.debug(f"Already subscribed to {contract.symbol}")
            return True
        
        try:
            # 使用 ib_insync 的 reqRealTimeBars
            # 参数: contract, barSize=5, whatToShow, useRTH
            bars = self.ib_adapter.ib.reqRealTimeBars(
                contract,
                barSize=5,  # 5 秒 K 线
                whatToShow='TRADES',
                useRTH=False  # 包含盘前盘后
            )
            
            # 注册回调
            bars.updateEvent += lambda bars, hasNewBar: self._on_realtime_bar(
                contract, bars, hasNewBar
            )
            
            self._subscriptions[contract.conId] = contract
            
            # 初始化该合约的聚合状态
            symbol = contract.symbol
            for tf in self.timeframes:
                self._current_bars[symbol][tf] = None
            
            logger.info(f"Subscribed to 5-second bars for {contract.symbol}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to subscribe realtime bars for {contract.symbol}: {e}")
            return False
    
    async def unsubscribe(self, contract: Contract) -> None:
        """取消订阅"""
        if contract.conId in self._subscriptions:
            try:
                self.ib_adapter.ib.cancelRealTimeBars(contract)
            except Exception as e:
                logger.warning(f"Error cancelling realtime bars: {e}")
            
            del self._subscriptions[contract.conId]
            
            # 清理聚合状态
            symbol = contract.symbol
            if symbol in self._current_bars:
                del self._current_bars[symbol]
            
            logger.info(f"Unsubscribed from 5-second bars for {contract.symbol}")
    
    def _on_realtime_bar(
        self,
        contract: Contract,
        bars: List[RealTimeBar],
        hasNewBar: bool
    ) -> None:
        """
        5 秒 K 线回调
        
        这是 ib_insync 的同步回调，需要快速处理
        """
        if not hasNewBar or not bars:
            return
        
        # 获取最新的 5 秒 K 线
        bar = bars[-1]
        symbol = contract.symbol
        
        # 对每个周期进行聚合
        for timeframe in self.timeframes:
            self._aggregate_bar(symbol, timeframe, bar)
    
    def _aggregate_bar(
        self,
        symbol: str,
        timeframe: str,
        bar: RealTimeBar
    ) -> None:
        """
        将 5 秒 K 线聚合到指定周期
        """
        period_seconds = AggregatorConfig.get_seconds(timeframe)
        
        # 计算当前 5 秒 K 线所属的周期开始时间
        bar_time = bar.time
        if isinstance(bar_time, datetime):
            timestamp = bar_time.timestamp()
        else:
            # 如果是 date 对象，转换
            timestamp = datetime.combine(bar_time, datetime.min.time()).timestamp()
        
        period_start_ts = (int(timestamp) // period_seconds) * period_seconds
        period_start = datetime.fromtimestamp(period_start_ts)
        
        current_bar = self._current_bars[symbol].get(timeframe)
        
        # 检查是否需要开始新的 K 线
        if current_bar is None or current_bar.timestamp != period_start:
            # 如果有旧的 K 线，标记为完成并发布
            if current_bar is not None:
                current_bar.is_complete = True
                self._publish_bar(current_bar)
            
            # 开始新的 K 线
            self._current_bars[symbol][timeframe] = BarData(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=period_start,
                open=bar.open_,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                is_complete=False
            )
        else:
            # 更新当前 K 线
            current_bar.high = max(current_bar.high, bar.high)
            current_bar.low = min(current_bar.low, bar.low)
            current_bar.close = bar.close
            current_bar.volume += bar.volume
    
    def _publish_bar(self, bar: BarData) -> None:
        """发布完成的 K 线"""
        try:
            from core.events import BarEvent
            
            event = BarEvent(
                symbol=bar.symbol,
                timeframe=bar.timeframe,
                bar_time=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                is_complete=bar.is_complete
            )
            
            # 使用同步发布（因为我们在回调中）
            self.event_bus.publish_sync(event)
            
            logger.debug(
                f"Bar completed: {bar.symbol} {bar.timeframe} "
                f"O={bar.open:.2f} H={bar.high:.2f} L={bar.low:.2f} C={bar.close:.2f}"
            )
            
        except Exception as e:
            logger.error(f"Failed to publish bar event: {e}")
    
    async def start(self) -> None:
        """启动聚合器"""
        self._running = True
        logger.info("BarAggregator started")
    
    async def stop(self) -> None:
        """停止聚合器"""
        self._running = False
        
        # 取消所有订阅
        for contract in list(self._subscriptions.values()):
            await self.unsubscribe(contract)
        
        # 发布所有未完成的 K 线
        for symbol, timeframes in self._current_bars.items():
            for timeframe, bar in timeframes.items():
                if bar is not None and not bar.is_complete:
                    bar.is_complete = True
                    self._publish_bar(bar)
        
        self._current_bars.clear()
        logger.info("BarAggregator stopped")
    
    def get_current_bar(self, symbol: str, timeframe: str) -> Optional[BarData]:
        """获取当前未完成的 K 线"""
        return self._current_bars.get(symbol, {}).get(timeframe)
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            "subscriptions": len(self._subscriptions),
            "symbols": list(self._current_bars.keys()),
            "timeframes": self.timeframes,
            "running": self._running
        }
