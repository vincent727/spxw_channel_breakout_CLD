"""
交易日历和时间工具

SPXW 0DTE 期权自动交易系统 V4

使用 pandas_market_calendars 自动判断交易日

功能:
1. 判断是否交易日
2. 获取下一个交易日
3. 判断是否在交易时间内
4. 获取 0DTE 期权的到期日
"""

from __future__ import annotations

import logging
from datetime import datetime, date, time, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import pandas as pd

logger = logging.getLogger(__name__)

# 美东时区
ET = ZoneInfo("America/New_York")


class TradingCalendar:
    """交易日历 - 使用 pandas_market_calendars"""
    
    def __init__(self, timezone: str = "America/New_York"):
        self.tz = ZoneInfo(timezone)
        
        # 使用 CBOE Index Options 日历（SPX 期权）
        # 可用选项: CBOE_Index_Options, CBOE_Equity_Options, NYSE
        self.calendar = mcal.get_calendar('CBOE_Index_Options')
        
        # 缓存交易日（提高性能）
        self._schedule_cache: Optional[pd.DataFrame] = None
        self._cache_start: Optional[date] = None
        self._cache_end: Optional[date] = None
        
        # 0DTE 交易时间限制
        self.dte_start_buffer = timedelta(minutes=5)  # 开盘后 5 分钟开始
        self.dte_end_buffer = timedelta(minutes=10)   # 收盘前 10 分钟结束
        
        logger.info("TradingCalendar initialized with CBOE_Index_Options calendar")
    
    def _ensure_schedule_cached(self, d: date) -> None:
        """确保日历数据已缓存"""
        # 缓存 30 天范围
        if (self._schedule_cache is None or 
            self._cache_start is None or 
            self._cache_end is None or
            d < self._cache_start or 
            d > self._cache_end):
            
            start = d - timedelta(days=7)
            end = d + timedelta(days=30)
            
            self._schedule_cache = self.calendar.schedule(
                start_date=start.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d")
            )
            self._cache_start = start
            self._cache_end = end
    
    def now_et(self) -> datetime:
        """获取当前美东时间"""
        return datetime.now(self.tz)
    
    def today_et(self) -> date:
        """获取今天日期 (美东)"""
        return self.now_et().date()
    
    def is_trading_day(self, d: date = None) -> bool:
        """
        判断是否交易日
        """
        if d is None:
            d = self.today_et()
        
        self._ensure_schedule_cached(d)
        
        # 检查日期是否在交易日历中
        date_str = d.strftime("%Y-%m-%d")
        return date_str in self._schedule_cache.index.strftime("%Y-%m-%d").tolist()
    
    def get_market_hours(self, d: date = None) -> Tuple[datetime, datetime]:
        """获取市场交易时间（返回带时区的 datetime）"""
        if d is None:
            d = self.today_et()
        
        self._ensure_schedule_cached(d)
        
        date_str = d.strftime("%Y-%m-%d")
        
        if date_str in self._schedule_cache.index.strftime("%Y-%m-%d").tolist():
            # 找到对应行
            idx = self._schedule_cache.index.strftime("%Y-%m-%d").tolist().index(date_str)
            row = self._schedule_cache.iloc[idx]
            
            market_open = row['market_open'].to_pydatetime()
            market_close = row['market_close'].to_pydatetime()
            
            return market_open, market_close
        
        # 默认时间
        return (
            datetime.combine(d, time(9, 30), tzinfo=self.tz),
            datetime.combine(d, time(16, 0), tzinfo=self.tz)
        )
    
    def is_early_close_day(self, d: date = None) -> bool:
        """判断是否半天交易日"""
        if d is None:
            d = self.today_et()
        
        if not self.is_trading_day(d):
            return False
        
        _, market_close = self.get_market_hours(d)
        
        # 如果收盘时间早于 16:00，则为半天交易日
        return market_close.time() < time(16, 0)
    
    def is_market_open(self, dt: datetime = None) -> bool:
        """
        判断市场是否开放
        """
        if dt is None:
            dt = self.now_et()
        
        d = dt.date()
        
        # 不是交易日
        if not self.is_trading_day(d):
            return False
        
        # 检查时间
        market_open, market_close = self.get_market_hours(d)
        
        # 转换为相同时区比较
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        
        return market_open <= dt < market_close
    
    def get_next_trading_day(self, d: date = None) -> date:
        """
        获取下一个交易日
        
        如果今天是交易日且市场未收盘，返回今天
        否则返回下一个交易日
        """
        if d is None:
            now = self.now_et()
            d = now.date()
            
            # 如果今天是交易日且市场未收盘，返回今天
            if self.is_trading_day(d):
                _, market_close = self.get_market_hours(d)
                if now < market_close:
                    return d
        
        # 查找下一个交易日
        self._ensure_schedule_cached(d)
        
        # 从缓存中找下一个交易日
        for i in range(1, 15):  # 最多查找 15 天
            next_day = d + timedelta(days=i)
            if self.is_trading_day(next_day):
                return next_day
        
        # 如果缓存不够，扩展缓存再查找
        self._cache_end = d + timedelta(days=30)
        self._schedule_cache = self.calendar.schedule(
            start_date=self._cache_start.strftime("%Y-%m-%d"),
            end_date=self._cache_end.strftime("%Y-%m-%d")
        )
        
        for i in range(1, 30):
            next_day = d + timedelta(days=i)
            if self.is_trading_day(next_day):
                return next_day
        
        logger.error(f"Could not find next trading day after {d}")
        return d + timedelta(days=1)
    
    def get_0dte_expiry(self) -> str:
        """
        获取 0DTE 期权到期日
        
        SPXW 每个交易日都有 0DTE 期权
        
        Returns:
            YYYYMMDD 格式的日期字符串
        """
        next_trading_day = self.get_next_trading_day()
        return next_trading_day.strftime("%Y%m%d")
    
    def is_0dte_trading_allowed(self) -> Tuple[bool, str]:
        """
        判断是否允许 0DTE 交易
        
        条件:
        1. 是交易日
        2. 在交易时间内
        3. 不在开盘/收盘缓冲期
        
        Returns:
            (allowed, reason)
        """
        now = self.now_et()
        d = now.date()
        
        # 不是交易日
        if not self.is_trading_day(d):
            next_day = self.get_next_trading_day(d)
            return False, f"Not a trading day. Next trading day: {next_day}"
        
        market_open, market_close = self.get_market_hours(d)
        
        # 市场未开盘
        if now < market_open:
            return False, f"Market not open yet. Opens at {market_open.strftime('%H:%M')}"
        
        # 市场已收盘
        if now >= market_close:
            next_day = self.get_next_trading_day(d)
            return False, f"Market closed. Next trading day: {next_day}"
        
        # 开盘缓冲期
        start_time = market_open + self.dte_start_buffer
        if now < start_time:
            wait_seconds = (start_time - now).total_seconds()
            return False, f"Waiting for opening buffer. {wait_seconds:.0f}s remaining"
        
        # 收盘缓冲期
        end_time = market_close - self.dte_end_buffer
        if now >= end_time:
            return False, f"Too close to market close. Trading stopped"
        
        return True, "Trading allowed"
    
    def seconds_to_market_open(self) -> float:
        """距离开盘的秒数 (负数表示已开盘)"""
        now = self.now_et()
        d = now.date()
        
        if not self.is_trading_day(d):
            d = self.get_next_trading_day(d)
        
        market_open, _ = self.get_market_hours(d)
        
        return (market_open - now).total_seconds()
    
    def seconds_to_market_close(self) -> float:
        """距离收盘的秒数 (负数表示已收盘)"""
        now = self.now_et()
        d = now.date()
        
        if not self.is_trading_day(d):
            return -1
        
        _, market_close = self.get_market_hours(d)
        
        return (market_close - now).total_seconds()
    
    def seconds_since_market_open(self) -> Optional[float]:
        """
        开盘后经过的秒数
        
        Returns:
            float: 开盘后的秒数 (负数表示未开盘)
            None: 非交易日
        """
        now = self.now_et()
        d = now.date()
        
        if not self.is_trading_day(d):
            return None
        
        market_open, _ = self.get_market_hours(d)
        
        return (now - market_open).total_seconds()
    
    def get_trading_status(self) -> dict:
        """获取交易状态概览"""
        now = self.now_et()
        d = now.date()
        
        is_trading_day = self.is_trading_day(d)
        is_market_open = self.is_market_open(now)
        allowed, reason = self.is_0dte_trading_allowed()
        
        return {
            "current_time_et": now.strftime("%Y-%m-%d %H:%M:%S %A"),  # 包含星期
            "is_trading_day": is_trading_day,
            "is_market_open": is_market_open,
            "is_early_close": self.is_early_close_day(d) if is_trading_day else False,
            "trading_allowed": allowed,
            "reason": reason,
            "next_trading_day": self.get_next_trading_day(d).strftime("%Y-%m-%d"),
            "0dte_expiry": self.get_0dte_expiry(),
            "seconds_to_open": self.seconds_to_market_open() if not is_market_open else None,
            "seconds_to_close": self.seconds_to_market_close() if is_market_open else None
        }


# 全局实例
_calendar: Optional[TradingCalendar] = None


def get_trading_calendar() -> TradingCalendar:
    """获取交易日历单例"""
    global _calendar
    if _calendar is None:
        _calendar = TradingCalendar()
    return _calendar


def get_0dte_expiry() -> str:
    """便捷函数：获取 0DTE 到期日"""
    return get_trading_calendar().get_0dte_expiry()


def is_trading_allowed() -> Tuple[bool, str]:
    """便捷函数：判断是否允许交易"""
    return get_trading_calendar().is_0dte_trading_allowed()

