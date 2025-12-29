"""
期权选择器模块 - 包含流动性检查

SPXW 0DTE 期权自动交易系统 V4

SPXW Tick Size 规则:
- 价格 < $3.00: 最小变动 $0.05
- 价格 >= $3.00: 最小变动 $0.10

职责:
1. 根据信号选择合适的期权合约
2. 流动性验证
3. Delta 筛选
4. 价格检查
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from ib_insync import Contract, Ticker

from core.config import OptionSelectionConfig, LiquidityConfig
from core.events import SignalEvent
from .price_utils import align_buy_price

logger = logging.getLogger(__name__)


@dataclass
class LiquidityCheck:
    """流动性检查结果"""
    is_liquid: bool
    reason: str = ""
    bid: float = 0.0
    ask: float = 0.0
    spread: float = 0.0
    spread_pct: float = 0.0
    bid_size: int = 0
    ask_size: int = 0


@dataclass
class OptionCandidate:
    """期权候选"""
    contract: Contract
    strike: float
    right: str
    bid: float
    ask: float
    mid: float
    spread: float
    spread_pct: float
    bid_size: int
    ask_size: int
    delta: Optional[float] = None
    score: float = 0.0  # 综合评分


class OptionSelector:
    """
    期权选择器
    
    职责:
    1. 根据信号方向选择 Call/Put
    2. 选择合适的行权价
    3. 验证流动性
    4. 选择最佳合约
    """
    
    def __init__(
        self,
        option_pool: 'OptionPool',
        ib_adapter: 'IBAdapter',
        config: OptionSelectionConfig
    ):
        self.pool = option_pool
        self.ib = ib_adapter
        self.config = config
        self.liquidity_config = config.liquidity
        
        logger.info(
            f"OptionSelector initialized: "
            f"strike_offset={config.strike_offset}, "
            f"prefer_otm={config.prefer_otm}"
        )
    
    async def select_option(
        self,
        signal: SignalEvent,
        spx_price: float,
        max_retries: int = 2
    ) -> Optional[OptionCandidate]:
        """
        根据信号选择最佳期权
        
        Args:
            signal: 交易信号
            spx_price: 当前 SPX 价格
            max_retries: 最大重试次数（市场数据可能需要时间刷新）
        
        Returns:
            最佳期权候选或 None
        """
        # 确定期权类型
        if signal.signal_type == "LONG_CALL":
            right = "C"
        elif signal.signal_type == "LONG_PUT":
            right = "P"
        else:
            logger.error(f"Unknown signal type: {signal.signal_type}")
            return None
        
        # 带重试的获取候选合约
        candidates = None
        for attempt in range(max_retries + 1):
            candidates = await self._get_candidates(right, spx_price)
            
            if candidates:
                break
            
            if attempt < max_retries:
                logger.debug(f"No candidates found, retry {attempt + 1}/{max_retries}...")
                await self.ib.sleep(0.5)  # 等待市场数据刷新
        
        if not candidates:
            logger.warning(f"No suitable {right} options found after {max_retries + 1} attempts")
            return None
        
        # 选择最佳
        best = self._select_best_candidate(candidates, signal)
        
        if best:
            logger.info(
                f"Selected option: {best.contract.localSymbol} "
                f"Strike={best.strike} Mid=${best.mid:.2f} "
                f"Spread={best.spread_pct:.1%}"
            )
        
        return best
    
    async def _get_candidates(
        self,
        right: str,
        spx_price: float
    ) -> List[OptionCandidate]:
        """获取候选合约列表"""
        candidates = []
        
        # 从期权池获取合约
        if right == "C":
            contracts = self.pool.get_all_calls()
        else:
            contracts = self.pool.get_all_puts()
        
        for contract in contracts:
            # 获取报价
            ticker = await self._get_quote(contract)
            if not ticker:
                continue
            
            # 检查基本报价 (增加 NaN 检查)
            import math
            bid = ticker.bid
            ask = ticker.ask
            
            # 跳过无效报价
            if bid is None or ask is None:
                logger.debug(f"Skipping {contract.localSymbol}: No bid/ask data")
                continue
            if isinstance(bid, float) and math.isnan(bid):
                logger.debug(f"Skipping {contract.localSymbol}: Bid is NaN")
                continue
            if isinstance(ask, float) and math.isnan(ask):
                logger.debug(f"Skipping {contract.localSymbol}: Ask is NaN")
                continue
            if bid <= 0 or ask <= 0:
                logger.debug(f"Skipping {contract.localSymbol}: Invalid bid=${bid} ask=${ask}")
                continue
            
            # 检查最低 bid
            if bid < self.config.min_bid:
                logger.debug(f"Skipping {contract.localSymbol}: Bid ${bid} < min ${self.config.min_bid}")
                continue
            
            mid = (bid + ask) / 2
            spread = ask - bid
            spread_pct = spread / mid if mid > 0 else 1.0
            
            # 流动性检查
            liquidity = self._check_liquidity(
                bid, ask, 
                ticker.bidSize or 0,
                ticker.askSize or 0
            )
            
            if not liquidity.is_liquid:
                logger.debug(
                    f"Skipping {contract.localSymbol}: {liquidity.reason}"
                )
                continue
            
            # 创建候选
            candidate = OptionCandidate(
                contract=contract,
                strike=contract.strike,
                right=right,
                bid=bid,
                ask=ask,
                mid=mid,
                spread=spread,
                spread_pct=spread_pct,
                bid_size=ticker.bidSize or 0,
                ask_size=ticker.askSize or 0
            )
            
            # 计算评分
            candidate.score = self._calculate_score(candidate, spx_price, right)
            
            candidates.append(candidate)
        
        return candidates
    
    async def _get_quote(self, contract: Contract) -> Optional[Ticker]:
        """获取合约报价"""
        # 先检查是否已订阅
        ticker = self.ib.get_ticker(contract)
        
        if ticker and ticker.bid and ticker.bid > 0:
            return ticker
        
        # 临时订阅获取报价
        ticker = await self.ib.subscribe_market_data(contract)
        
        if ticker:
            # 等待报价填充 (增加等待时间)
            await self.ib.sleep(0.5)
            return self.ib.get_ticker(contract)
        
        return None
    
    def _check_liquidity(
        self,
        bid: float,
        ask: float,
        bid_size: int,
        ask_size: int
    ) -> LiquidityCheck:
        """
        检查流动性
        
        检查条件:
        1. Bid-Ask 价差绝对值
        2. Bid-Ask 价差百分比
        3. Bid/Ask 挂单量
        """
        config = self.liquidity_config
        
        spread = ask - bid
        mid = (bid + ask) / 2 if (bid + ask) > 0 else 0
        spread_pct = spread / mid if mid > 0 else 1.0
        
        # 检查价差绝对值
        if spread > config.max_bid_ask_spread:
            return LiquidityCheck(
                is_liquid=False,
                reason=f"Spread ${spread:.2f} > max ${config.max_bid_ask_spread}",
                bid=bid, ask=ask, spread=spread, spread_pct=spread_pct,
                bid_size=bid_size, ask_size=ask_size
            )
        
        # 检查价差百分比
        if spread_pct > config.max_spread_pct:
            return LiquidityCheck(
                is_liquid=False,
                reason=f"Spread {spread_pct:.1%} > max {config.max_spread_pct:.1%}",
                bid=bid, ask=ask, spread=spread, spread_pct=spread_pct,
                bid_size=bid_size, ask_size=ask_size
            )
        
        # 检查挂单量
        if bid_size < config.min_bid_size:
            return LiquidityCheck(
                is_liquid=False,
                reason=f"Bid size {bid_size} < min {config.min_bid_size}",
                bid=bid, ask=ask, spread=spread, spread_pct=spread_pct,
                bid_size=bid_size, ask_size=ask_size
            )
        
        if ask_size < config.min_ask_size:
            return LiquidityCheck(
                is_liquid=False,
                reason=f"Ask size {ask_size} < min {config.min_ask_size}",
                bid=bid, ask=ask, spread=spread, spread_pct=spread_pct,
                bid_size=bid_size, ask_size=ask_size
            )
        
        return LiquidityCheck(
            is_liquid=True,
            bid=bid, ask=ask, spread=spread, spread_pct=spread_pct,
            bid_size=bid_size, ask_size=ask_size
        )
    
    def _calculate_score(
        self,
        candidate: OptionCandidate,
        spx_price: float,
        right: str
    ) -> float:
        """
        计算候选评分
        
        评分因素:
        1. 流动性（价差越小越好）
        2. 行权价位置（偏好 OTM）
        3. 价格适中
        """
        score = 0.0
        
        # 流动性评分 (0-40)
        # 价差越小分数越高
        if candidate.spread_pct <= 0.02:
            score += 40
        elif candidate.spread_pct <= 0.03:
            score += 35
        elif candidate.spread_pct <= 0.04:
            score += 30
        elif candidate.spread_pct <= 0.05:
            score += 25
        else:
            score += 20
        
        # 行权价位置评分 (0-30)
        strike = candidate.strike
        
        if self.config.prefer_otm:
            if right == "C":
                # Call: 高于 SPX 为 OTM
                if strike > spx_price:
                    # OTM 程度适中最好
                    otm_pct = (strike - spx_price) / spx_price
                    if 0 < otm_pct <= 0.005:  # 0-0.5% OTM
                        score += 30
                    elif otm_pct <= 0.01:  # 0.5-1% OTM
                        score += 25
                    else:
                        score += 15
                else:
                    score += 10  # ITM
            else:
                # Put: 低于 SPX 为 OTM
                if strike < spx_price:
                    otm_pct = (spx_price - strike) / spx_price
                    if 0 < otm_pct <= 0.005:
                        score += 30
                    elif otm_pct <= 0.01:
                        score += 25
                    else:
                        score += 15
                else:
                    score += 10  # ITM
        else:
            # 偏好 ATM
            distance = abs(strike - spx_price) / spx_price
            if distance <= 0.002:  # 非常接近 ATM
                score += 30
            elif distance <= 0.005:
                score += 25
            else:
                score += 15
        
        # 价格评分 (0-30)
        # 价格适中（不太贵也不太便宜）
        mid = candidate.mid
        if 1.0 <= mid <= 5.0:
            score += 30
        elif 0.5 <= mid < 1.0 or 5.0 < mid <= 10.0:
            score += 25
        elif mid < 0.5:
            score += 15  # 太便宜可能流动性差
        else:
            score += 20  # 太贵
        
        return score
    
    def _select_best_candidate(
        self,
        candidates: List[OptionCandidate],
        signal: SignalEvent
    ) -> Optional[OptionCandidate]:
        """选择最佳候选"""
        if not candidates:
            return None
        
        # 按评分排序
        candidates.sort(key=lambda c: c.score, reverse=True)
        
        # 返回最高分
        return candidates[0]
    
    def calculate_entry_price(
        self,
        candidate: OptionCandidate,
        aggressive: bool = False
    ) -> float:
        """
        计算入场价格 (使用 SPXW tick size 对齐)
        
        Args:
            candidate: 期权候选
            aggressive: 是否激进（更快成交）
        
        Returns:
            对齐到有效 tick size 的限价
        """
        if aggressive:
            # 激进: 直接用 Ask 确保成交 (Ask 应该已经是有效价格)
            return align_buy_price(candidate.ask)
        else:
            # 保守: Mid + buffer，然后对齐到 tick size
            buffer = getattr(self.config, 'limit_price_buffer', 0.05)
            raw_price = candidate.mid + buffer
            return align_buy_price(raw_price)
    
    def validate_contract_for_trade(
        self,
        contract: Contract
    ) -> Tuple[bool, str]:
        """
        验证合约是否可交易
        
        Returns:
            (is_valid, reason)
        """
        # 检查合约是否在池中
        if not self.pool.get_contract(contract.strike, contract.right):
            return False, "Contract not in option pool"
        
        # 可以添加更多验证...
        
        return True, ""
