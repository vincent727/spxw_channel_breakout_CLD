"""
状态管理模块 - 使用 aiosqlite 进行异步状态持久化

SPXW 0DTE 期权自动交易系统 V4

特性:
- 异步数据库操作
- 交易记录持久化
- 仓位状态管理
- 系统状态恢复
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal
from contextlib import asynccontextmanager

import aiosqlite

logger = logging.getLogger(__name__)


# ============================================================================
# 数据模型
# ============================================================================

@dataclass
class Position:
    """持仓记录"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    contract_id: int = 0
    contract_symbol: str = ""
    direction: Literal["LONG_CALL", "LONG_PUT"] = "LONG_CALL"
    quantity: int = 0
    entry_price: float = 0.0
    entry_time: datetime = field(default_factory=datetime.now)
    current_price: float = 0.0
    highest_price: float = 0.0
    unrealized_pnl: float = 0.0
    unrealized_pnl_pct: float = 0.0
    
    # 止损状态
    breakeven_active: bool = False
    breakeven_price: float = 0.0
    trailing_active: bool = False
    trailing_stop_price: float = 0.0
    
    # 元数据
    signal_id: str = ""
    entry_order_id: int = 0
    status: Literal["OPEN", "CLOSING", "CLOSED"] = "OPEN"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        data = asdict(self)
        data['entry_time'] = self.entry_time.isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Position':
        """从字典创建"""
        if isinstance(data.get('entry_time'), str):
            data['entry_time'] = datetime.fromisoformat(data['entry_time'])
        return cls(**data)


@dataclass
class Trade:
    """交易记录"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    position_id: str = ""
    contract_id: int = 0
    contract_symbol: str = ""
    direction: Literal["LONG_CALL", "LONG_PUT"] = "LONG_CALL"
    
    # 入场
    entry_time: datetime = field(default_factory=datetime.now)
    entry_price: float = 0.0
    entry_order_id: int = 0
    
    # 出场
    exit_time: Optional[datetime] = None
    exit_price: float = 0.0
    exit_order_id: int = 0
    exit_reason: str = ""
    
    # 数量和盈亏
    quantity: int = 0
    realized_pnl: float = 0.0
    realized_pnl_pct: float = 0.0
    commission: float = 0.0
    
    # 止损信息
    stop_type: str = ""
    chase_phase: str = ""
    chase_count: int = 0
    
    # 状态
    status: Literal["OPEN", "CLOSED"] = "OPEN"
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        data = asdict(self)
        data['entry_time'] = self.entry_time.isoformat()
        if self.exit_time:
            data['exit_time'] = self.exit_time.isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Trade':
        """从字典创建"""
        if isinstance(data.get('entry_time'), str):
            data['entry_time'] = datetime.fromisoformat(data['entry_time'])
        if isinstance(data.get('exit_time'), str):
            data['exit_time'] = datetime.fromisoformat(data['exit_time'])
        return cls(**data)


@dataclass
class OrderRecord:
    """订单记录"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    order_id: int = 0
    contract_id: int = 0
    contract_symbol: str = ""
    action: Literal["BUY", "SELL"] = "BUY"
    order_type: str = "LMT"
    quantity: int = 0
    limit_price: float = 0.0
    
    # 状态
    status: str = "Submitted"
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    
    # 时间
    submit_time: datetime = field(default_factory=datetime.now)
    fill_time: Optional[datetime] = None
    
    # 关联
    position_id: str = ""
    is_stop_order: bool = False


@dataclass
class DailyStats:
    """每日统计"""
    date: str = ""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    max_profit: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    created_at: Optional[str] = None  # 数据库自动生成


# ============================================================================
# 交易状态管理器
# ============================================================================

class TradingState:
    """
    交易状态管理器
    
    职责:
    1. 管理当前持仓
    2. 记录交易历史
    3. 持久化到 SQLite
    4. 系统重启后恢复状态
    """
    
    def __init__(self, db_path: str = "data/trading_state.db"):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        
        # 内存缓存
        self._positions: Dict[str, Position] = {}
        self._trades: Dict[str, Trade] = {}
        self._orders: Dict[int, OrderRecord] = {}  # order_id -> OrderRecord
        
        # 每日统计
        self._daily_stats: Optional[DailyStats] = None
        self._today: str = ""
        
        # 熔断状态
        self._circuit_breaker_active: bool = False
        self._circuit_breaker_until: Optional[datetime] = None
    
    async def initialize(self) -> None:
        """初始化数据库"""
        # 确保目录存在
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        
        await self._create_tables()
        await self._load_state()
        
        logger.info(f"TradingState initialized: {self.db_path}")
    
    async def _create_tables(self) -> None:
        """创建数据库表"""
        await self._db.executescript("""
            -- 持仓表
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                contract_id INTEGER,
                contract_symbol TEXT,
                direction TEXT,
                quantity INTEGER,
                entry_price REAL,
                entry_time TEXT,
                current_price REAL,
                highest_price REAL,
                unrealized_pnl REAL,
                unrealized_pnl_pct REAL,
                breakeven_active INTEGER,
                breakeven_price REAL,
                trailing_active INTEGER,
                trailing_stop_price REAL,
                signal_id TEXT,
                entry_order_id INTEGER,
                status TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 交易表
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                position_id TEXT,
                contract_id INTEGER,
                contract_symbol TEXT,
                direction TEXT,
                entry_time TEXT,
                entry_price REAL,
                entry_order_id INTEGER,
                exit_time TEXT,
                exit_price REAL,
                exit_order_id INTEGER,
                exit_reason TEXT,
                quantity INTEGER,
                realized_pnl REAL,
                realized_pnl_pct REAL,
                commission REAL,
                stop_type TEXT,
                chase_phase TEXT,
                chase_count INTEGER,
                status TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 订单表
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                order_id INTEGER UNIQUE,
                contract_id INTEGER,
                contract_symbol TEXT,
                action TEXT,
                order_type TEXT,
                quantity INTEGER,
                limit_price REAL,
                status TEXT,
                filled_qty INTEGER,
                avg_fill_price REAL,
                submit_time TEXT,
                fill_time TEXT,
                position_id TEXT,
                is_stop_order INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 每日统计表
            CREATE TABLE IF NOT EXISTS daily_stats (
                date TEXT PRIMARY KEY,
                total_trades INTEGER,
                winning_trades INTEGER,
                losing_trades INTEGER,
                total_pnl REAL,
                max_drawdown REAL,
                max_profit REAL,
                win_rate REAL,
                avg_win REAL,
                avg_loss REAL,
                profit_factor REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 系统状态表
            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            
            -- 创建索引
            CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_trades_date ON trades(entry_time);
            CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id);
        """)
        await self._db.commit()
    
    async def _load_state(self) -> None:
        """加载状态"""
        # 加载开放仓位
        async with self._db.execute(
            "SELECT * FROM positions WHERE status = 'OPEN'"
        ) as cursor:
            async for row in cursor:
                pos = Position.from_dict(dict(row))
                self._positions[pos.id] = pos
        
        logger.info(f"Loaded {len(self._positions)} open positions")
        
        # 加载今日统计
        today = date.today().isoformat()
        async with self._db.execute(
            "SELECT * FROM daily_stats WHERE date = ?", (today,)
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                self._daily_stats = DailyStats(**dict(row))
                self._today = today
    
    async def close(self) -> None:
        """关闭数据库连接"""
        if self._db:
            await self._db.close()
            self._db = None
    
    # ========================================================================
    # 仓位管理
    # ========================================================================
    
    async def add_position(self, position: Position) -> None:
        """添加新仓位"""
        self._positions[position.id] = position
        
        await self._db.execute("""
            INSERT INTO positions (
                id, contract_id, contract_symbol, direction, quantity,
                entry_price, entry_time, current_price, highest_price,
                unrealized_pnl, unrealized_pnl_pct, breakeven_active,
                breakeven_price, trailing_active, trailing_stop_price,
                signal_id, entry_order_id, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            position.id, position.contract_id, position.contract_symbol,
            position.direction, position.quantity, position.entry_price,
            position.entry_time.isoformat(), position.current_price,
            position.highest_price, position.unrealized_pnl,
            position.unrealized_pnl_pct, int(position.breakeven_active),
            position.breakeven_price, int(position.trailing_active),
            position.trailing_stop_price, position.signal_id,
            position.entry_order_id, position.status
        ))
        await self._db.commit()
        
        logger.info(f"Position added: {position.id} - {position.contract_symbol}")
    
    async def update_position(self, position: Position) -> None:
        """更新仓位"""
        self._positions[position.id] = position
        
        await self._db.execute("""
            UPDATE positions SET
                current_price = ?,
                highest_price = ?,
                unrealized_pnl = ?,
                unrealized_pnl_pct = ?,
                breakeven_active = ?,
                breakeven_price = ?,
                trailing_active = ?,
                trailing_stop_price = ?,
                status = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (
            position.current_price, position.highest_price,
            position.unrealized_pnl, position.unrealized_pnl_pct,
            int(position.breakeven_active), position.breakeven_price,
            int(position.trailing_active), position.trailing_stop_price,
            position.status, position.id
        ))
        await self._db.commit()
    
    async def close_position(self, position_id: str) -> Optional[Position]:
        """关闭仓位"""
        position = self._positions.pop(position_id, None)
        
        if position:
            position.status = "CLOSED"
            await self._db.execute(
                "UPDATE positions SET status = 'CLOSED', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (position_id,)
            )
            await self._db.commit()
            logger.info(f"Position closed: {position_id}")
        
        return position
    
    def get_position(self, position_id: str) -> Optional[Position]:
        """获取仓位"""
        return self._positions.get(position_id)
    
    def get_position_by_contract(self, contract_id: int) -> Optional[Position]:
        """根据合约ID获取仓位"""
        for pos in self._positions.values():
            if pos.contract_id == contract_id and pos.status == "OPEN":
                return pos
        return None
    
    def get_all_positions(self) -> List[Position]:
        """获取所有开放仓位"""
        return [p for p in self._positions.values() if p.status == "OPEN"]
    
    def get_total_position_count(self) -> int:
        """获取当前持仓数量"""
        return len([p for p in self._positions.values() if p.status == "OPEN"])
    
    def get_total_position_value(self) -> float:
        """获取总持仓价值"""
        return sum(
            p.current_price * p.quantity * 100  # SPX 期权乘数 100
            for p in self._positions.values()
            if p.status == "OPEN"
        )
    
    # ========================================================================
    # 交易记录
    # ========================================================================
    
    async def add_trade(self, trade: Trade) -> None:
        """添加交易记录"""
        self._trades[trade.id] = trade
        
        await self._db.execute("""
            INSERT INTO trades (
                id, position_id, contract_id, contract_symbol, direction,
                entry_time, entry_price, entry_order_id, exit_time, exit_price,
                exit_order_id, exit_reason, quantity, realized_pnl,
                realized_pnl_pct, commission, stop_type, chase_phase,
                chase_count, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.id, trade.position_id, trade.contract_id, trade.contract_symbol,
            trade.direction, trade.entry_time.isoformat(), trade.entry_price,
            trade.entry_order_id, 
            trade.exit_time.isoformat() if trade.exit_time else None,
            trade.exit_price, trade.exit_order_id, trade.exit_reason,
            trade.quantity, trade.realized_pnl, trade.realized_pnl_pct,
            trade.commission, trade.stop_type, trade.chase_phase,
            trade.chase_count, trade.status
        ))
        await self._db.commit()
        
        # 更新每日统计
        await self._update_daily_stats(trade)
    
    async def update_trade(self, trade: Trade) -> None:
        """更新交易记录"""
        self._trades[trade.id] = trade
        
        await self._db.execute("""
            UPDATE trades SET
                exit_time = ?,
                exit_price = ?,
                exit_order_id = ?,
                exit_reason = ?,
                realized_pnl = ?,
                realized_pnl_pct = ?,
                commission = ?,
                stop_type = ?,
                chase_phase = ?,
                chase_count = ?,
                status = ?
            WHERE id = ?
        """, (
            trade.exit_time.isoformat() if trade.exit_time else None,
            trade.exit_price, trade.exit_order_id, trade.exit_reason,
            trade.realized_pnl, trade.realized_pnl_pct, trade.commission,
            trade.stop_type, trade.chase_phase, trade.chase_count,
            trade.status, trade.id
        ))
        await self._db.commit()
    
    async def get_trades_by_date(self, trade_date: str) -> List[Trade]:
        """获取指定日期的交易"""
        trades = []
        async with self._db.execute(
            "SELECT * FROM trades WHERE date(entry_time) = ?",
            (trade_date,)
        ) as cursor:
            async for row in cursor:
                trades.append(Trade.from_dict(dict(row)))
        return trades
    
    async def get_recent_trades(self, limit: int = 50) -> List[Trade]:
        """获取最近的交易"""
        trades = []
        async with self._db.execute(
            "SELECT * FROM trades ORDER BY entry_time DESC LIMIT ?",
            (limit,)
        ) as cursor:
            async for row in cursor:
                trades.append(Trade.from_dict(dict(row)))
        return trades
    
    # ========================================================================
    # 订单管理
    # ========================================================================
    
    async def add_order(self, order: OrderRecord) -> None:
        """添加订单记录"""
        self._orders[order.order_id] = order
        
        await self._db.execute("""
            INSERT INTO orders (
                id, order_id, contract_id, contract_symbol, action,
                order_type, quantity, limit_price, status, filled_qty,
                avg_fill_price, submit_time, fill_time, position_id, is_stop_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order.id, order.order_id, order.contract_id, order.contract_symbol,
            order.action, order.order_type, order.quantity, order.limit_price,
            order.status, order.filled_qty, order.avg_fill_price,
            order.submit_time.isoformat(),
            order.fill_time.isoformat() if order.fill_time else None,
            order.position_id, int(order.is_stop_order)
        ))
        await self._db.commit()
    
    async def update_order_status(
        self,
        order_id: int,
        status: str,
        filled_qty: int = 0,
        avg_fill_price: float = 0.0
    ) -> None:
        """更新订单状态"""
        if order_id in self._orders:
            order = self._orders[order_id]
            order.status = status
            order.filled_qty = filled_qty
            order.avg_fill_price = avg_fill_price
            if status == "Filled":
                order.fill_time = datetime.now()
        
        await self._db.execute("""
            UPDATE orders SET
                status = ?,
                filled_qty = ?,
                avg_fill_price = ?,
                fill_time = ?
            WHERE order_id = ?
        """, (
            status, filled_qty, avg_fill_price,
            datetime.now().isoformat() if status == "Filled" else None,
            order_id
        ))
        await self._db.commit()
    
    def get_order(self, order_id: int) -> Optional[OrderRecord]:
        """获取订单"""
        return self._orders.get(order_id)
    
    # ========================================================================
    # 每日统计
    # ========================================================================
    
    async def _update_daily_stats(self, trade: Trade) -> None:
        """更新每日统计"""
        today = date.today().isoformat()
        
        if self._today != today or self._daily_stats is None:
            self._today = today
            self._daily_stats = DailyStats(date=today)
        
        stats = self._daily_stats
        stats.total_trades += 1
        stats.total_pnl += trade.realized_pnl
        
        if trade.realized_pnl > 0:
            stats.winning_trades += 1
            stats.max_profit = max(stats.max_profit, trade.realized_pnl)
        else:
            stats.losing_trades += 1
            stats.max_drawdown = min(stats.max_drawdown, trade.realized_pnl)
        
        # 计算胜率
        if stats.total_trades > 0:
            stats.win_rate = stats.winning_trades / stats.total_trades
        
        # 保存到数据库
        await self._db.execute("""
            INSERT OR REPLACE INTO daily_stats (
                date, total_trades, winning_trades, losing_trades,
                total_pnl, max_drawdown, max_profit, win_rate,
                avg_win, avg_loss, profit_factor
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            stats.date, stats.total_trades, stats.winning_trades,
            stats.losing_trades, stats.total_pnl, stats.max_drawdown,
            stats.max_profit, stats.win_rate, stats.avg_win,
            stats.avg_loss, stats.profit_factor
        ))
        await self._db.commit()
    
    def get_daily_stats(self) -> Optional[DailyStats]:
        """获取今日统计"""
        return self._daily_stats
    
    def get_today_pnl(self) -> float:
        """获取今日 PnL"""
        if self._daily_stats:
            return self._daily_stats.total_pnl
        return 0.0
    
    def get_today_trades(self) -> List[Trade]:
        """获取今日交易列表"""
        today = date.today().isoformat()
        return [t for t in self._trades.values() 
                if t.entry_time.date().isoformat() == today]
    
    async def get_stats_by_date_range(
        self,
        start_date: str,
        end_date: str
    ) -> List[DailyStats]:
        """获取日期范围内的统计"""
        stats = []
        async with self._db.execute(
            "SELECT * FROM daily_stats WHERE date >= ? AND date <= ? ORDER BY date",
            (start_date, end_date)
        ) as cursor:
            async for row in cursor:
                stats.append(DailyStats(**dict(row)))
        return stats
    
    # ========================================================================
    # 熔断状态
    # ========================================================================
    
    def is_circuit_breaker_active(self) -> bool:
        """检查熔断是否激活"""
        if not self._circuit_breaker_active:
            return False
        
        if self._circuit_breaker_until and datetime.now() > self._circuit_breaker_until:
            self._circuit_breaker_active = False
            return False
        
        return True
    
    async def activate_circuit_breaker(self, until: datetime, reason: str) -> None:
        """激活熔断"""
        self._circuit_breaker_active = True
        self._circuit_breaker_until = until
        
        await self._save_system_state("circuit_breaker_active", "true")
        await self._save_system_state("circuit_breaker_until", until.isoformat())
        await self._save_system_state("circuit_breaker_reason", reason)
        
        logger.warning(f"Circuit breaker activated until {until}: {reason}")
    
    async def deactivate_circuit_breaker(self) -> None:
        """解除熔断"""
        self._circuit_breaker_active = False
        self._circuit_breaker_until = None
        
        await self._save_system_state("circuit_breaker_active", "false")
    
    # ========================================================================
    # 系统状态
    # ========================================================================
    
    async def _save_system_state(self, key: str, value: str) -> None:
        """保存系统状态"""
        await self._db.execute("""
            INSERT OR REPLACE INTO system_state (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (key, value))
        await self._db.commit()
    
    async def _load_system_state(self, key: str) -> Optional[str]:
        """加载系统状态"""
        async with self._db.execute(
            "SELECT value FROM system_state WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row['value'] if row else None


# 全局状态实例
_state: Optional[TradingState] = None


async def get_state() -> TradingState:
    """获取全局状态实例"""
    global _state
    if _state is None:
        _state = TradingState()
        await _state.initialize()
    return _state


def set_state(state: TradingState) -> None:
    """设置全局状态实例"""
    global _state
    _state = state
