"""
价格工具模块 - SPXW Tick Size 对齐

SPXW 0DTE 期权自动交易系统 V4

SPXW Tick Size 规则:
- 价格 < $3.00: 最小变动 $0.05
- 价格 >= $3.00: 最小变动 $0.10

这是 CBOE 的规则，限价单必须符合 tick size 否则会被拒绝。
"""

from __future__ import annotations

import math
from decimal import Decimal, ROUND_UP, ROUND_DOWN, ROUND_HALF_UP
from typing import Literal


# SPXW Tick Size 阈值
TICK_SIZE_THRESHOLD = 3.00
TICK_SIZE_BELOW_3 = 0.05
TICK_SIZE_3_AND_ABOVE = 0.10


def get_tick_size(price: float) -> float:
    """
    获取给定价格的 tick size
    
    Args:
        price: 期权价格
        
    Returns:
        tick size ($0.05 或 $0.10)
    """
    if price < TICK_SIZE_THRESHOLD:
        return TICK_SIZE_BELOW_3
    return TICK_SIZE_3_AND_ABOVE


def align_price(
    price: float,
    direction: Literal["up", "down", "nearest"] = "nearest"
) -> float:
    """
    将价格对齐到有效的 tick size
    
    Args:
        price: 原始价格
        direction: 对齐方向
            - "up": 向上取整（买入时使用，确保成交）
            - "down": 向下取整（卖出时使用，确保成交）
            - "nearest": 四舍五入到最近的 tick
            
    Returns:
        对齐后的价格
        
    Examples:
        >>> align_price(2.47, "up")    # < $3, tick=0.05
        2.50
        >>> align_price(2.47, "down")
        2.45
        >>> align_price(4.53, "up")    # >= $3, tick=0.10
        4.60
        >>> align_price(4.53, "down")
        4.50
    """
    if price <= 0:
        return 0.0
    
    tick_size = get_tick_size(price)
    
    # 使用 Decimal 确保精度
    d_price = Decimal(str(price))
    d_tick = Decimal(str(tick_size))
    
    if direction == "up":
        # 向上取整
        aligned = (d_price / d_tick).quantize(Decimal('1'), rounding=ROUND_UP) * d_tick
    elif direction == "down":
        # 向下取整
        aligned = (d_price / d_tick).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_tick
    else:  # nearest
        # 四舍五入
        aligned = (d_price / d_tick).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * d_tick
    
    return float(aligned)


def align_buy_price(price: float) -> float:
    """
    对齐买入价格（向上取整确保成交）
    
    Args:
        price: 原始价格
        
    Returns:
        对齐后的买入限价
    """
    return align_price(price, direction="up")


def align_sell_price(price: float) -> float:
    """
    对齐卖出价格（向下取整确保成交）
    
    Args:
        price: 原始价格
        
    Returns:
        对齐后的卖出限价
    """
    return align_price(price, direction="down")


def is_valid_price(price: float) -> bool:
    """
    检查价格是否符合 tick size 规则
    
    Args:
        price: 价格
        
    Returns:
        是否为有效价格
    """
    if price <= 0:
        return False
    
    tick_size = get_tick_size(price)
    
    # 使用 Decimal 检查
    d_price = Decimal(str(price))
    d_tick = Decimal(str(tick_size))
    
    # 检查是否是 tick size 的整数倍
    remainder = d_price % d_tick
    return remainder == 0


def calculate_entry_price(mid: float, buffer: float) -> float:
    """
    计算入场限价（买入）
    
    Args:
        mid: 中间价
        buffer: 价格缓冲（愿意多付的金额）
        
    Returns:
        对齐后的入场限价
    """
    raw_price = mid + buffer
    return align_buy_price(raw_price)


def calculate_exit_price(mid: float, buffer: float = 0.0) -> float:
    """
    计算出场限价（卖出）
    
    Args:
        mid: 中间价
        buffer: 价格缓冲（愿意少收的金额）
        
    Returns:
        对齐后的出场限价
    """
    raw_price = mid - buffer
    return align_sell_price(raw_price)


def calculate_chase_price(
    current_ask: float,
    chase_buffer: float,
    is_buy: bool = True
) -> float:
    """
    计算追单价格
    
    Args:
        current_ask: 当前卖一价（买入时）或买一价（卖出时）
        chase_buffer: 追单缓冲
        is_buy: 是否为买入订单
        
    Returns:
        对齐后的追单限价
    """
    if is_buy:
        raw_price = current_ask + chase_buffer
        return align_buy_price(raw_price)
    else:
        raw_price = current_ask - chase_buffer
        return align_sell_price(raw_price)


# 测试代码
if __name__ == "__main__":
    # 测试 tick size
    print("=== Tick Size Tests ===")
    print(f"get_tick_size(2.50) = {get_tick_size(2.50)}")  # 0.05
    print(f"get_tick_size(3.00) = {get_tick_size(3.00)}")  # 0.10
    print(f"get_tick_size(5.50) = {get_tick_size(5.50)}")  # 0.10
    
    # 测试价格对齐
    print("\n=== Price Alignment Tests ===")
    test_prices = [2.47, 2.52, 2.975, 3.05, 4.53, 4.57, 10.23]
    
    for p in test_prices:
        tick = get_tick_size(p)
        up = align_price(p, "up")
        down = align_price(p, "down")
        nearest = align_price(p, "nearest")
        print(f"Price={p:.3f} (tick={tick}): up={up:.2f}, down={down:.2f}, nearest={nearest:.2f}")
    
    # 测试有效性检查
    print("\n=== Validity Tests ===")
    valid_prices = [2.50, 2.55, 3.00, 3.10, 5.50]
    invalid_prices = [2.47, 2.52, 3.05, 4.53]
    
    for p in valid_prices:
        print(f"is_valid_price({p:.2f}) = {is_valid_price(p)}")
    for p in invalid_prices:
        print(f"is_valid_price({p:.3f}) = {is_valid_price(p)}")
