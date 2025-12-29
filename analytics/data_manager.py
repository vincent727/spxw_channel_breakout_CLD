"""
数据管理模块 - 分层存储策略

SPXW 0DTE 期权自动交易系统 V4

存储策略:
- 交易状态、订单、持仓 -> aiosqlite (低频写入，需要事务)
- 实时 Tick -> 仅内存 (用于计算，不持久化)
- Tick 快照 -> 追加写 CSV/JSON (仅关键时刻记录)
- K 线历史 -> Parquet 文件 (列式存储，高压缩)
"""

from __future__ import annotations

import csv
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class TickData:
    """Tick 数据结构"""
    contract_id: int
    symbol: str
    last: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    bid_size: Optional[int] = None
    ask_size: Optional[int] = None
    volume: Optional[int] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class BarData:
    """K线数据结构"""
    symbol: str
    timeframe: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    timestamp: datetime


class DataManager:
    """
    分层数据管理器
    
    职责:
    1. Tick 数据内存缓存
    2. 关键 Tick 快照写入
    3. K 线数据管理
    4. 历史数据加载
    """
    
    def __init__(self, db_path: str = "data/trading_state.db",
                 tick_snapshot_path: str = "data/tick_snapshots/",
                 historical_data_path: str = "data/historical/",
                 tick_buffer_size: int = 1000):
        
        # Tick 数据 - 仅内存缓存
        self.tick_buffer: Dict[int, Deque[TickData]] = {}
        self.tick_buffer_size = tick_buffer_size
        
        # 最新 Tick
        self.latest_tick: Dict[int, TickData] = {}
        
        # Tick 快照日志路径
        self.tick_snapshot_path = Path(tick_snapshot_path)
        self.tick_snapshot_path.mkdir(parents=True, exist_ok=True)
        self._tick_snapshot_file: Optional[Path] = None
        self._init_tick_snapshot_file()
        
        # K 线缓存
        self.bar_buffer: Dict[str, Deque[BarData]] = {}  # symbol_timeframe -> bars
        self.bar_buffer_size = 500  # 保留最近 500 根 K 线
        
        # 历史数据路径
        self.historical_path = Path(historical_data_path)
        self.historical_path.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"DataManager initialized: tick_buffer={self.tick_buffer_size}")
    
    def _init_tick_snapshot_file(self) -> None:
        """初始化 Tick 快照文件"""
        today = datetime.now().strftime("%Y%m%d")
        self._tick_snapshot_file = self.tick_snapshot_path / f"tick_snapshots_{today}.csv"
        
        # 如果文件不存在，写入表头
        if not self._tick_snapshot_file.exists():
            with open(self._tick_snapshot_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'reason', 'contract_id', 'symbol',
                    'last', 'bid', 'ask', 'bid_size', 'ask_size', 'volume',
                    'context'
                ])
    
    # ========================================================================
    # Tick 数据管理
    # ========================================================================
    
    def buffer_tick(self, tick: TickData) -> None:
        """
        缓存 Tick 到内存（用于计算）
        
        注意: 不写入磁盘，仅内存缓存
        """
        contract_id = tick.contract_id
        
        if contract_id not in self.tick_buffer:
            self.tick_buffer[contract_id] = deque(maxlen=self.tick_buffer_size)
        
        self.tick_buffer[contract_id].append(tick)
        self.latest_tick[contract_id] = tick
    
    def snapshot_tick(
        self,
        tick: TickData,
        reason: str,
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        快照关键 Tick（止损触发、异常价格等）
        
        仅在关键时刻写入，避免高频 I/O
        """
        # 检查日期是否变化，需要新文件
        today = datetime.now().strftime("%Y%m%d")
        expected_file = self.tick_snapshot_path / f"tick_snapshots_{today}.csv"
        if expected_file != self._tick_snapshot_file:
            self._init_tick_snapshot_file()
        
        try:
            with open(self._tick_snapshot_file, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    tick.timestamp.isoformat(),
                    reason,
                    tick.contract_id,
                    tick.symbol,
                    tick.last,
                    tick.bid,
                    tick.ask,
                    tick.bid_size,
                    tick.ask_size,
                    tick.volume,
                    json.dumps(context) if context else ""
                ])
        except Exception as e:
            logger.error(f"Failed to write tick snapshot: {e}")
    
    def get_recent_ticks(
        self,
        contract_id: int,
        count: int = 100
    ) -> List[TickData]:
        """获取最近的 Tick 数据"""
        if contract_id not in self.tick_buffer:
            return []
        
        buffer = self.tick_buffer[contract_id]
        return list(buffer)[-count:]
    
    def get_latest_tick(self, contract_id: int) -> Optional[TickData]:
        """获取最新 Tick"""
        return self.latest_tick.get(contract_id)
    
    def clear_tick_buffer(self, contract_id: int) -> None:
        """清除指定合约的 Tick 缓存"""
        if contract_id in self.tick_buffer:
            self.tick_buffer[contract_id].clear()
        if contract_id in self.latest_tick:
            del self.latest_tick[contract_id]
    
    # ========================================================================
    # K 线数据管理
    # ========================================================================
    
    def buffer_bar(self, bar: BarData) -> None:
        """缓存 K 线"""
        key = f"{bar.symbol}_{bar.timeframe}"
        
        if key not in self.bar_buffer:
            self.bar_buffer[key] = deque(maxlen=self.bar_buffer_size)
        
        self.bar_buffer[key].append(bar)
    
    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100
    ) -> List[BarData]:
        """获取 K 线数据"""
        key = f"{symbol}_{timeframe}"
        
        if key not in self.bar_buffer:
            return []
        
        return list(self.bar_buffer[key])[-count:]
    
    def get_bars_df(
        self,
        symbol: str,
        timeframe: str,
        count: int = 100
    ) -> pd.DataFrame:
        """获取 K 线数据为 DataFrame"""
        bars = self.get_bars(symbol, timeframe, count)
        
        if not bars:
            return pd.DataFrame()
        
        return pd.DataFrame([
            {
                'timestamp': b.timestamp,
                'open': b.open,
                'high': b.high,
                'low': b.low,
                'close': b.close,
                'volume': b.volume
            }
            for b in bars
        ]).set_index('timestamp')
    
    def get_latest_bar(self, symbol: str, timeframe: str) -> Optional[BarData]:
        """获取最新 K 线"""
        key = f"{symbol}_{timeframe}"
        
        if key not in self.bar_buffer or not self.bar_buffer[key]:
            return None
        
        return self.bar_buffer[key][-1]
    
    # ========================================================================
    # 历史数据加载
    # ========================================================================
    
    def load_historical_bars(
        self,
        symbol: str,
        timeframe: str,
        start_date: str,
        end_date: str
    ) -> pd.DataFrame:
        """
        加载历史 K 线数据
        
        支持 Parquet 和 CSV 格式
        """
        # 构建文件路径
        file_base = f"{symbol}_{timeframe.replace(' ', '_')}"
        
        # 尝试 Parquet
        parquet_file = self.historical_path / f"{file_base}.parquet"
        if parquet_file.exists():
            df = pd.read_parquet(parquet_file)
            return self._filter_date_range(df, start_date, end_date)
        
        # 尝试 CSV
        csv_file = self.historical_path / f"{file_base}.csv"
        if csv_file.exists():
            df = pd.read_csv(csv_file, parse_dates=['timestamp'])
            df.set_index('timestamp', inplace=True)
            return self._filter_date_range(df, start_date, end_date)
        
        logger.warning(f"No historical data found for {symbol} {timeframe}")
        return pd.DataFrame()
    
    def _filter_date_range(
        self,
        df: pd.DataFrame,
        start_date: str,
        end_date: str
    ) -> pd.DataFrame:
        """过滤日期范围"""
        if df.empty:
            return df
        
        mask = (df.index >= start_date) & (df.index <= end_date)
        return df[mask]
    
    def save_historical_bars(
        self,
        df: pd.DataFrame,
        symbol: str,
        timeframe: str,
        format: str = "parquet"
    ) -> None:
        """保存历史 K 线数据"""
        file_base = f"{symbol}_{timeframe.replace(' ', '_')}"
        
        if format == "parquet":
            file_path = self.historical_path / f"{file_base}.parquet"
            df.to_parquet(file_path)
        else:
            file_path = self.historical_path / f"{file_base}.csv"
            df.to_csv(file_path)
        
        logger.info(f"Saved historical data: {file_path}")
    
    # ========================================================================
    # 统计和清理
    # ========================================================================
    
    def get_stats(self) -> Dict[str, Any]:
        """获取数据管理器统计"""
        tick_counts = {
            cid: len(buf) for cid, buf in self.tick_buffer.items()
        }
        bar_counts = {
            key: len(buf) for key, buf in self.bar_buffer.items()
        }
        
        return {
            "tick_buffer_contracts": len(self.tick_buffer),
            "tick_counts": tick_counts,
            "bar_buffer_keys": len(self.bar_buffer),
            "bar_counts": bar_counts,
            "tick_snapshot_file": str(self._tick_snapshot_file)
        }
    
    def clear_all_buffers(self) -> None:
        """清除所有缓存"""
        self.tick_buffer.clear()
        self.latest_tick.clear()
        self.bar_buffer.clear()
        logger.info("All data buffers cleared")
