"""
通知分发器模块

SPXW 0DTE 期权自动交易系统 V4

功能:
1. 事件驱动的通知分发
2. 支持多种通知渠道
3. 通知模板管理
4. 优先级处理
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
from enum import Enum

from core.event_bus import EventBus
from core.events import (
    NotificationEvent, SignalEvent, FillEvent, 
    StopTriggeredEvent, CircuitBreakerEvent,
    StopExecutionResultEvent, ConnectionEvent
)
from core.config import NotificationConfig

logger = logging.getLogger(__name__)


class NotificationPriority(Enum):
    """通知优先级"""
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


@dataclass
class Notification:
    """通知消息"""
    notification_type: str
    title: str
    message: str
    priority: NotificationPriority
    timestamp: datetime
    data: Optional[Dict[str, Any]] = None


class NotificationChannel(ABC):
    """通知渠道基类"""
    
    @abstractmethod
    async def send(self, notification: Notification) -> bool:
        """发送通知"""
        pass
    
    @abstractmethod
    def is_enabled(self) -> bool:
        """是否启用"""
        pass


class NotificationDispatcher:
    """
    通知分发器
    
    功能:
    1. 订阅各类交易事件
    2. 根据事件类型生成通知
    3. 分发到各通知渠道
    """
    
    def __init__(
        self,
        config: NotificationConfig,
        event_bus: EventBus
    ):
        self.config = config
        self.event_bus = event_bus
        
        # 通知渠道
        self.channels: List[NotificationChannel] = []
        
        # 通知队列
        self.notification_queue: asyncio.Queue = asyncio.Queue()
        
        # 处理任务
        self._processor_task: Optional[asyncio.Task] = None
        self._running: bool = False
        
        # 订阅事件
        self._setup_event_subscriptions()
        
        logger.info("NotificationDispatcher initialized")
    
    def _setup_event_subscriptions(self) -> None:
        """设置事件订阅"""
        # 信号事件
        if self.config.events.get("signal", True):
            self.event_bus.subscribe(SignalEvent, self._on_signal)
        
        # 成交事件
        if self.config.events.get("fill", True):
            self.event_bus.subscribe(FillEvent, self._on_fill)
        
        # 止损触发
        if self.config.events.get("stop_triggered", True):
            self.event_bus.subscribe(StopTriggeredEvent, self._on_stop_triggered)
        
        # 止损执行结果
        if self.config.events.get("stop_result", True):
            self.event_bus.subscribe(StopExecutionResultEvent, self._on_stop_result)
        
        # 熔断器
        if self.config.events.get("circuit_breaker", True):
            self.event_bus.subscribe(CircuitBreakerEvent, self._on_circuit_breaker)
        
        # 连接状态
        if self.config.events.get("connection", True):
            self.event_bus.subscribe(ConnectionEvent, self._on_connection)
        
        # 通用通知事件
        self.event_bus.subscribe(NotificationEvent, self._on_notification)
    
    def add_channel(self, channel: NotificationChannel) -> None:
        """添加通知渠道"""
        self.channels.append(channel)
        logger.info(f"Notification channel added: {type(channel).__name__}")
    
    async def start(self) -> None:
        """启动通知处理"""
        self._running = True
        self._processor_task = asyncio.create_task(self._process_notifications())
        logger.info("NotificationDispatcher started")
    
    async def stop(self) -> None:
        """停止通知处理"""
        self._running = False
        if self._processor_task:
            self._processor_task.cancel()
            try:
                await self._processor_task
            except asyncio.CancelledError:
                pass
        logger.info("NotificationDispatcher stopped")
    
    # ========================================================================
    # 事件处理
    # ========================================================================
    
    async def _on_signal(self, event: SignalEvent) -> None:
        """信号事件处理"""
        notification = Notification(
            notification_type="signal",
            title="📈 交易信号",
            message=self._format_signal_message(event),
            priority=NotificationPriority.NORMAL,
            timestamp=datetime.now(),
            data={"signal_type": event.signal_type}
        )
        await self.notification_queue.put(notification)
    
    async def _on_fill(self, event: FillEvent) -> None:
        """成交事件处理"""
        action = "开仓" if event.is_entry else "平仓"
        notification = Notification(
            notification_type="fill",
            title=f"✅ 订单成交 - {action}",
            message=self._format_fill_message(event),
            priority=NotificationPriority.HIGH,
            timestamp=datetime.now(),
            data={
                "order_id": event.order_id,
                "is_entry": event.is_entry
            }
        )
        await self.notification_queue.put(notification)
    
    async def _on_stop_triggered(self, event: StopTriggeredEvent) -> None:
        """止损触发事件处理"""
        notification = Notification(
            notification_type="stop_triggered",
            title="🛑 止损触发",
            message=self._format_stop_triggered_message(event),
            priority=NotificationPriority.HIGH,
            timestamp=datetime.now(),
            data={
                "position_id": event.position_id,
                "stop_type": event.stop_type
            }
        )
        await self.notification_queue.put(notification)
    
    async def _on_stop_result(self, event: StopExecutionResultEvent) -> None:
        """止损执行结果处理"""
        if event.success:
            title = "✅ 止损完成"
            priority = NotificationPriority.NORMAL
        elif event.phase == "ABANDONED":
            title = "⚠️ 止损放弃"
            priority = NotificationPriority.HIGH
        else:
            title = "❌ 止损失败"
            priority = NotificationPriority.URGENT
        
        notification = Notification(
            notification_type="stop_result",
            title=title,
            message=self._format_stop_result_message(event),
            priority=priority,
            timestamp=datetime.now(),
            data={"phase": event.phase}
        )
        await self.notification_queue.put(notification)
    
    async def _on_circuit_breaker(self, event: CircuitBreakerEvent) -> None:
        """熔断器事件处理"""
        notification = Notification(
            notification_type="circuit_breaker",
            title="🚨 熔断触发",
            message=self._format_circuit_breaker_message(event),
            priority=NotificationPriority.URGENT,
            timestamp=datetime.now(),
            data={"trigger_type": event.trigger_type}
        )
        await self.notification_queue.put(notification)
    
    async def _on_connection(self, event: ConnectionEvent) -> None:
        """连接状态事件处理"""
        if event.status == "DISCONNECTED":
            title = "⚠️ 连接断开"
            priority = NotificationPriority.URGENT
        elif event.status == "RECONNECTING":
            title = "🔄 正在重连"
            priority = NotificationPriority.HIGH
        else:
            title = "✅ 连接恢复"
            priority = NotificationPriority.NORMAL
        
        notification = Notification(
            notification_type="connection",
            title=title,
            message=event.message,
            priority=priority,
            timestamp=datetime.now()
        )
        await self.notification_queue.put(notification)
    
    async def _on_notification(self, event: NotificationEvent) -> None:
        """通用通知事件处理"""
        priority = NotificationPriority(event.priority_level)
        
        notification = Notification(
            notification_type=event.notification_type,
            title=event.title,
            message=event.message,
            priority=priority,
            timestamp=datetime.now()
        )
        await self.notification_queue.put(notification)
    
    # ========================================================================
    # 消息格式化
    # ========================================================================
    
    def _format_signal_message(self, event: SignalEvent) -> str:
        """格式化信号消息"""
        return (
            f"信号类型: {event.signal_type}\n"
            f"SPX 价格: ${event.spx_price:.2f}\n"
            f"通道: [{event.channel_lower:.2f}, {event.channel_upper:.2f}]\n"
            f"市场状态: {event.regime}\n"
            f"信号强度: {event.strength:.2f}"
        )
    
    def _format_fill_message(self, event: FillEvent) -> str:
        """格式化成交消息"""
        action = "买入" if event.action == "BUY" else "卖出"
        return (
            f"操作: {action}\n"
            f"合约: {event.contract_symbol}\n"
            f"数量: {event.quantity}\n"
            f"成交价: ${event.fill_price:.2f}\n"
            f"佣金: ${event.commission:.2f}"
        )
    
    def _format_stop_triggered_message(self, event: StopTriggeredEvent) -> str:
        """格式化止损触发消息"""
        return (
            f"合约: {event.contract_symbol}\n"
            f"止损类型: {event.stop_type}\n"
            f"触发价格: ${event.trigger_price:.2f}\n"
            f"入场价格: ${event.entry_price:.2f}\n"
            f"浮动盈亏: ${event.unrealized_pnl:.2f} ({event.unrealized_pnl_pct:.1%})"
        )
    
    def _format_stop_result_message(self, event: StopExecutionResultEvent) -> str:
        """格式化止损结果消息"""
        lines = [
            f"持仓ID: {event.position_id}",
            f"执行阶段: {event.phase}",
            f"追单次数: {event.chase_count}",
            f"执行时间: {event.total_duration_ms:.0f}ms"
        ]
        
        if event.fill_price:
            lines.append(f"成交价格: ${event.fill_price:.2f}")
        
        if event.pnl:
            lines.append(f"实现盈亏: ${event.pnl:.2f} ({event.pnl_pct:.1%})")
        
        return "\n".join(lines)
    
    def _format_circuit_breaker_message(self, event: CircuitBreakerEvent) -> str:
        """格式化熔断消息"""
        return (
            f"触发类型: {event.trigger_type}\n"
            f"触发值: {event.trigger_value}\n"
            f"阈值: {event.threshold}\n"
            f"恢复时间: {event.cooldown_until.strftime('%H:%M:%S')}\n"
            f"消息: {event.message}"
        )
    
    # ========================================================================
    # 通知处理
    # ========================================================================
    
    async def _process_notifications(self) -> None:
        """处理通知队列"""
        while self._running:
            try:
                # 等待通知
                notification = await asyncio.wait_for(
                    self.notification_queue.get(),
                    timeout=1.0
                )
                
                # 分发到各渠道
                await self._dispatch_notification(notification)
                
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing notification: {e}")
    
    async def _dispatch_notification(self, notification: Notification) -> None:
        """分发通知到各渠道"""
        for channel in self.channels:
            if not channel.is_enabled():
                continue
            
            try:
                success = await channel.send(notification)
                if not success:
                    logger.warning(
                        f"Failed to send notification via {type(channel).__name__}"
                    )
            except Exception as e:
                logger.error(
                    f"Error sending notification via {type(channel).__name__}: {e}"
                )
    
    async def send_notification(
        self,
        notification_type: str,
        title: str,
        message: str,
        priority: str = "normal"
    ) -> None:
        """手动发送通知"""
        notification = Notification(
            notification_type=notification_type,
            title=title,
            message=message,
            priority=NotificationPriority(priority),
            timestamp=datetime.now()
        )
        await self.notification_queue.put(notification)
