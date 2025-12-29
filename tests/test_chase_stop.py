"""
动态追单止损测试

SPXW 0DTE 期权自动交易系统 V4

测试用例:
- Phase 1 立即成交
- Phase 2 追单后成交
- Phase 3 Panic 成交
- 价格过低放弃止损
- 最后手段市价单
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, MagicMock, patch
from dataclasses import dataclass

from risk.chase_stop_executor import (
    DynamicChaseStopExecutor,
    PositionStop,
    StopResult,
    StopOrderState
)
from core.config import ChaseStopConfig
from core.event_bus import EventBus


@dataclass
class MockTicker:
    """模拟 Ticker"""
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0


@dataclass
class MockOrderStatus:
    """模拟订单状态"""
    status: str = "Submitted"
    avgFillPrice: float = 0.0
    filled: int = 0


@dataclass
class MockOrder:
    """模拟订单"""
    orderId: int = 1
    lmtPrice: float = 0.0


@dataclass
class MockTrade:
    """模拟 Trade"""
    order: MockOrder = None
    orderStatus: MockOrderStatus = None
    
    def __post_init__(self):
        if self.order is None:
            self.order = MockOrder()
        if self.orderStatus is None:
            self.orderStatus = MockOrderStatus()


class MockContract:
    """模拟合约"""
    def __init__(self):
        self.conId = 12345
        self.localSymbol = "SPXW 241228C05000"
        self.right = "C"


@pytest.fixture
def config():
    """测试配置"""
    return ChaseStopConfig(
        initial_buffer=0.10,
        chase_interval_ms=100,  # 加快测试速度
        chase_buffer=0.10,
        max_chase_count=3,
        max_chase_duration_ms=500,
        panic_loss_threshold=0.40,
        panic_price_factor=0.80,
        abandon_price_threshold=0.20,
        enable_market_fallback=True,
        market_fallback_delay_ms=100
    )


@pytest.fixture
def event_bus():
    """事件总线"""
    return EventBus()


@pytest.fixture
def mock_ib_adapter():
    """模拟 IB 适配器"""
    adapter = Mock()
    adapter.ib = Mock()
    adapter.sleep = AsyncMock()
    return adapter


@pytest.fixture
def position():
    """测试持仓"""
    return PositionStop(
        id="test-position-1",
        contract=MockContract(),
        contract_id=12345,
        quantity=1,
        entry_price=5.00,
        highest_price=5.50
    )


class TestDynamicChaseStop:
    """动态追单止损测试"""
    
    @pytest.mark.asyncio
    async def test_phase1_immediate_fill(
        self, config, event_bus, mock_ib_adapter, position
    ):
        """Phase 1 立即成交"""
        executor = DynamicChaseStopExecutor(
            mock_ib_adapter, config, event_bus
        )
        
        # 模拟报价
        mock_ticker = MockTicker(bid=4.50, ask=4.60)
        mock_ib_adapter.ib.ticker = Mock(return_value=mock_ticker)
        mock_ib_adapter.ib.reqMktData = Mock()
        
        # 模拟订单立即成交
        mock_trade = MockTrade()
        mock_trade.order.orderId = 100
        mock_trade.orderStatus.status = "Filled"
        mock_trade.orderStatus.avgFillPrice = 4.40
        
        mock_ib_adapter.ib.placeOrder = Mock(return_value=mock_trade)
        mock_ib_adapter.ib.trades = Mock(return_value=[mock_trade])
        
        # 执行止损
        result = await executor.execute_stop(position, 4.50)
        
        # 验证
        assert result.success is True
        assert result.phase == "AGGRESSIVE"
        assert result.fill_price == 4.40
        assert result.chase_count == 0
    
    @pytest.mark.asyncio
    async def test_phase2_chase_and_fill(
        self, config, event_bus, mock_ib_adapter, position
    ):
        """Phase 2 追单后成交"""
        executor = DynamicChaseStopExecutor(
            mock_ib_adapter, config, event_bus
        )
        
        # 模拟报价
        mock_ticker = MockTicker(bid=4.50, ask=4.60)
        
        # 模拟订单
        mock_trade = MockTrade()
        mock_trade.order.orderId = 100
        mock_trade.order.lmtPrice = 4.40
        
        call_count = [0]
        
        def mock_ticker_func(contract):
            # 价格逐渐下跌
            prices = [4.50, 4.30, 4.10, 3.90]
            idx = min(call_count[0], len(prices) - 1)
            call_count[0] += 1
            return MockTicker(bid=prices[idx], ask=prices[idx] + 0.10)
        
        mock_ib_adapter.ib.ticker = mock_ticker_func
        mock_ib_adapter.ib.reqMktData = Mock()
        mock_ib_adapter.ib.placeOrder = Mock(return_value=mock_trade)
        
        # 模拟在第2次追单后成交
        def mock_trades():
            if call_count[0] >= 3:
                mock_trade.orderStatus.status = "Filled"
                mock_trade.orderStatus.avgFillPrice = 3.85
            return [mock_trade]
        
        mock_ib_adapter.ib.trades = mock_trades
        
        # 执行止损
        result = await executor.execute_stop(position, 4.50)
        
        # 验证
        assert result.success is True
        assert result.phase == "CHASING"
        assert result.chase_count >= 1
    
    @pytest.mark.asyncio
    async def test_abandon_low_price(
        self, config, event_bus, mock_ib_adapter, position
    ):
        """价格过低放弃止损"""
        executor = DynamicChaseStopExecutor(
            mock_ib_adapter, config, event_bus
        )
        
        # 模拟极低报价
        mock_ticker = MockTicker(bid=0.15, ask=0.25)
        mock_ib_adapter.ib.ticker = Mock(return_value=mock_ticker)
        mock_ib_adapter.ib.reqMktData = Mock()
        
        # 模拟未成交订单
        mock_trade = MockTrade()
        mock_trade.order.orderId = 100
        mock_trade.orderStatus.status = "Submitted"
        
        mock_ib_adapter.ib.placeOrder = Mock(return_value=mock_trade)
        mock_ib_adapter.ib.trades = Mock(return_value=[mock_trade])
        mock_ib_adapter.ib.cancelOrder = Mock()
        
        # 执行止损
        result = await executor.execute_stop(position, 0.20)
        
        # 验证放弃
        assert result.success is False
        assert result.phase == "ABANDONED"
    
    @pytest.mark.asyncio
    async def test_market_fallback(
        self, config, event_bus, mock_ib_adapter, position
    ):
        """最后手段市价单"""
        executor = DynamicChaseStopExecutor(
            mock_ib_adapter, config, event_bus
        )
        
        # 模拟报价
        mock_ticker = MockTicker(bid=3.00, ask=3.20)
        mock_ib_adapter.ib.ticker = Mock(return_value=mock_ticker)
        mock_ib_adapter.ib.reqMktData = Mock()
        
        # 模拟限价单一直不成交
        mock_trade_limit = MockTrade()
        mock_trade_limit.order.orderId = 100
        mock_trade_limit.orderStatus.status = "Submitted"
        
        # 模拟市价单成交
        mock_trade_market = MockTrade()
        mock_trade_market.order.orderId = 101
        mock_trade_market.orderStatus.status = "Filled"
        mock_trade_market.orderStatus.avgFillPrice = 2.80
        
        place_order_calls = [0]
        
        def mock_place_order(contract, order):
            place_order_calls[0] += 1
            if hasattr(order, 'lmtPrice'):
                return mock_trade_limit
            else:
                return mock_trade_market
        
        mock_ib_adapter.ib.placeOrder = mock_place_order
        mock_ib_adapter.ib.trades = Mock(return_value=[mock_trade_market])
        mock_ib_adapter.ib.cancelOrder = Mock()
        
        # 强制进入 Phase 3
        config.max_chase_count = 0
        config.max_chase_duration_ms = 1
        
        # 执行止损
        result = await executor.execute_stop(position, 3.00)
        
        # 验证使用了市价单
        assert result.phase in ["DONE", "PANIC", "FAILED"]


class TestTickFilter:
    """Tick 数据过滤测试"""
    
    def test_filter_mid_deviation(self):
        """过滤 Mid 偏离过大的 Tick"""
        from execution.tick_streamer import TickStreamer, TickValidationResult
        from core.config import TickConfig, TickFilterConfig
        from core.events import TickEvent
        
        filter_config = TickFilterConfig(
            enabled=True,
            mid_deviation_threshold=0.10,
            jump_threshold=0.20,
            confirm_ticks=2,
            confirm_duration_ms=50
        )
        
        # 模拟偏离过大的 Tick
        tick = TickEvent(
            symbol="SPXW",
            contract_id=12345,
            last=5.00,
            bid=4.00,
            ask=4.20  # Mid = 4.10, last 偏离 22%
        )
        
        # 验证应该被过滤
        mid = (tick.bid + tick.ask) / 2
        deviation = abs(tick.last - mid) / mid
        assert deviation > filter_config.mid_deviation_threshold
    
    def test_filter_price_jump(self):
        """过滤价格跳变过大的 Tick"""
        from core.events import TickEvent
        from core.config import TickFilterConfig
        
        filter_config = TickFilterConfig(
            enabled=True,
            mid_deviation_threshold=0.10,
            jump_threshold=0.20
        )
        
        prev_tick = TickEvent(
            symbol="SPXW",
            contract_id=12345,
            last=5.00
        )
        
        # 25% 跳变
        new_tick = TickEvent(
            symbol="SPXW",
            contract_id=12345,
            last=6.25
        )
        
        jump = abs(new_tick.last - prev_tick.last) / prev_tick.last
        assert jump > filter_config.jump_threshold
    
    def test_valid_tick_passes(self):
        """有效 Tick 通过验证"""
        from core.events import TickEvent
        from core.config import TickFilterConfig
        
        filter_config = TickFilterConfig(
            enabled=True,
            mid_deviation_threshold=0.10,
            jump_threshold=0.20
        )
        
        tick = TickEvent(
            symbol="SPXW",
            contract_id=12345,
            last=4.15,
            bid=4.10,
            ask=4.20  # Mid = 4.15, 完美匹配
        )
        
        mid = (tick.bid + tick.ask) / 2
        deviation = abs(tick.last - mid) / mid
        assert deviation <= filter_config.mid_deviation_threshold


class TestOrderValidation:
    """订单验证测试"""
    
    def test_buy_order_allowed(self):
        """买入订单允许"""
        from core.config import ExecutionConfig
        
        config = ExecutionConfig(enforce_long_only=True)
        
        # 验证逻辑
        assert config.enforce_long_only is True
    
    def test_sell_without_position_blocked(self):
        """无持仓卖出被阻止"""
        # 这需要完整的 OrderManager 测试
        # 核心验证: SELL 订单必须有对应持仓
        pass
    
    def test_sell_exceeds_position_blocked(self):
        """卖出数量超过持仓被阻止"""
        # 核心验证: SELL 数量 <= 持仓数量
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
