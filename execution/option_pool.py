"""
期权池模块 - 动态预加载期权合约

SPXW 0DTE 期权自动交易系统 V4

设计目标:
- 信号触发时，合约已在内存中，延迟从秒级降到毫秒级
- 自动跟踪 SPX 价格变动，动态刷新池
- 预加载 ATM 附近的期权合约
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, date
from typing import Dict, List, Optional, Tuple

from ib_insync import Contract

from core.event_bus import EventBus
from core.events import OptionPoolRefreshEvent, SPXPriceEvent
from core.config import OptionPoolConfig

logger = logging.getLogger(__name__)


@dataclass
class CachedContract:
    """缓存的合约信息"""
    contract: Contract
    strike: float
    right: str  # "C" or "P"
    loaded_at: datetime
    last_quote_time: Optional[datetime] = None
    bid: Optional[float] = None
    ask: Optional[float] = None


class OptionPool:
    """
    动态期权池 - 预加载 ATM 附近合约
    
    特性:
    1. 启动时加载 ATM 附近合约
    2. SPX 价格变动时自动刷新
    3. 定期刷新（防止过期）
    4. 毫秒级合约获取
    """
    
    def __init__(
        self,
        ib_adapter: 'IBAdapter',  # 延迟导入避免循环
        config: OptionPoolConfig,
        event_bus: EventBus
    ):
        self.ib = ib_adapter
        self.config = config
        self.event_bus = event_bus
        
        # 合约池: (strike, right) -> CachedContract
        self.pool: Dict[Tuple[float, str], CachedContract] = {}
        
        # 刷新状态
        self.last_refresh_price: float = 0.0
        self.last_refresh_time: Optional[datetime] = None
        self.current_atm_strike: float = 0.0
        
        # 是否正在刷新
        self._refreshing: bool = False
        
        # 订阅 SPX 价格事件
        self.event_bus.subscribe(SPXPriceEvent, self._on_spx_price_update)
        
        logger.info(
            f"OptionPool initialized: strikes_around_atm={config.strikes_around_atm}, "
            f"refresh_threshold={config.refresh_price_threshold}"
        )
    
    async def ensure_ready(self, spx_price: float) -> None:
        """
        确保期权池已准备好
        
        在交易开始前调用
        """
        if self._should_refresh(spx_price):
            await self._refresh_pool(spx_price)
    
    def _should_refresh(self, spx_price: float) -> bool:
        """判断是否需要刷新池"""
        if not self.config.enabled:
            return False
        
        # 首次加载
        if not self.pool:
            return True
        
        # 价格变动超过阈值
        if abs(spx_price - self.last_refresh_price) > self.config.refresh_price_threshold:
            return True
        
        # 时间间隔超过阈值
        if self.last_refresh_time:
            elapsed = (datetime.now() - self.last_refresh_time).total_seconds()
            if elapsed > self.config.refresh_interval_seconds:
                return True
        
        return False
    
    async def _refresh_pool(self, spx_price: float) -> None:
        """刷新期权池"""
        if self._refreshing:
            logger.debug("Pool refresh already in progress")
            return
        
        self._refreshing = True
        
        try:
            # 检查有效价格
            import math
            if math.isnan(spx_price) or spx_price <= 0:
                logger.warning(f"Invalid SPX price for refresh: {spx_price}")
                return
            
            # 计算 ATM 行权价 (SPX 行权价间隔 5)
            atm_strike = round(spx_price / 5) * 5
            self.current_atm_strike = atm_strike
            
            logger.info(
                f"Refreshing option pool: SPX={spx_price:.2f}, ATM={atm_strike}"
            )
            
            new_pool: Dict[Tuple[float, str], CachedContract] = {}
            loaded_count = 0
            
            # 加载 ATM 附近的合约
            offsets = range(
                -self.config.strikes_around_atm,
                self.config.strikes_around_atm + 1
            )
            
            for offset in offsets:
                strike = atm_strike + offset * 5
                
                for right in ["C", "P"]:
                    try:
                        contract = await self.ib.resolve_option_contract(
                            symbol="SPXW",
                            strike=strike,
                            right=right,
                            expiry="today"
                        )
                        
                        if contract:
                            cached = CachedContract(
                                contract=contract,
                                strike=strike,
                                right=right,
                                loaded_at=datetime.now()
                            )
                            new_pool[(strike, right)] = cached
                            loaded_count += 1
                            
                    except Exception as e:
                        logger.warning(
                            f"Failed to resolve contract {strike}{right}: {e}"
                        )
            
            self.pool = new_pool
            self.last_refresh_price = spx_price
            self.last_refresh_time = datetime.now()
            
            logger.info(
                f"Option pool refreshed: {loaded_count} contracts around ATM={atm_strike}"
            )
            
            # 发布刷新事件
            await self.event_bus.publish(OptionPoolRefreshEvent(
                atm_strike=atm_strike,
                contracts_loaded=loaded_count,
                refresh_reason="price_change" if self.pool else "initial"
            ))
            
        except Exception as e:
            logger.error(f"Failed to refresh option pool: {e}")
        finally:
            self._refreshing = False
    
    async def _on_spx_price_update(self, event: SPXPriceEvent) -> None:
        """SPX 价格更新回调"""
        if self._should_refresh(event.price):
            await self._refresh_pool(event.price)
    
    # ========================================================================
    # 合约获取 - 毫秒级
    # ========================================================================
    
    def get_contract(self, strike: float, right: str) -> Optional[Contract]:
        """
        从池中获取合约（毫秒级）
        
        Args:
            strike: 行权价
            right: "C" for Call, "P" for Put
        
        Returns:
            Contract 或 None
        """
        cached = self.pool.get((strike, right))
        return cached.contract if cached else None
    
    def get_atm_call(self) -> Optional[Contract]:
        """获取 ATM Call"""
        return self.get_contract(self.current_atm_strike, "C")
    
    def get_atm_put(self) -> Optional[Contract]:
        """获取 ATM Put"""
        return self.get_contract(self.current_atm_strike, "P")
    
    def get_otm_call(self, offset: int = 1) -> Optional[Contract]:
        """获取 OTM Call (高于 ATM)"""
        strike = self.current_atm_strike + offset * 5
        return self.get_contract(strike, "C")
    
    def get_otm_put(self, offset: int = 1) -> Optional[Contract]:
        """获取 OTM Put (低于 ATM)"""
        strike = self.current_atm_strike - offset * 5
        return self.get_contract(strike, "P")
    
    def get_all_calls(self) -> List[Contract]:
        """获取所有 Call 合约"""
        return [
            cached.contract
            for (strike, right), cached in self.pool.items()
            if right == "C"
        ]
    
    def get_all_puts(self) -> List[Contract]:
        """获取所有 Put 合约"""
        return [
            cached.contract
            for (strike, right), cached in self.pool.items()
            if right == "P"
        ]
    
    def get_contract_by_criteria(
        self,
        right: str,
        min_strike: Optional[float] = None,
        max_strike: Optional[float] = None
    ) -> List[Contract]:
        """
        根据条件获取合约
        """
        contracts = []
        
        for (strike, r), cached in self.pool.items():
            if r != right:
                continue
            
            if min_strike and strike < min_strike:
                continue
            
            if max_strike and strike > max_strike:
                continue
            
            contracts.append(cached.contract)
        
        return contracts
    
    # ========================================================================
    # 报价更新
    # ========================================================================
    
    def update_quote(
        self,
        strike: float,
        right: str,
        bid: Optional[float],
        ask: Optional[float]
    ) -> None:
        """更新合约报价"""
        key = (strike, right)
        if key in self.pool:
            cached = self.pool[key]
            cached.bid = bid
            cached.ask = ask
            cached.last_quote_time = datetime.now()
    
    def get_quote(self, strike: float, right: str) -> Tuple[Optional[float], Optional[float]]:
        """获取合约报价"""
        cached = self.pool.get((strike, right))
        if cached:
            return cached.bid, cached.ask
        return None, None
    
    # ========================================================================
    # 状态和统计
    # ========================================================================
    
    def get_pool_stats(self) -> Dict:
        """获取期权池统计"""
        calls = [c for (s, r), c in self.pool.items() if r == "C"]
        puts = [c for (s, r), c in self.pool.items() if r == "P"]
        
        return {
            "total_contracts": len(self.pool),
            "calls": len(calls),
            "puts": len(puts),
            "current_atm": self.current_atm_strike,
            "last_refresh_price": self.last_refresh_price,
            "last_refresh_time": self.last_refresh_time.isoformat() if self.last_refresh_time else None,
            "strikes": sorted(list(set(s for s, r in self.pool.keys())))
        }
    
    def is_ready(self) -> bool:
        """检查池是否准备好"""
        return len(self.pool) > 0
    
    def clear(self) -> None:
        """清空池"""
        self.pool.clear()
        self.last_refresh_price = 0.0
        self.last_refresh_time = None
        logger.info("Option pool cleared")
