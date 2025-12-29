"""
事件总线模块 - 发布/订阅模式实现

SPXW 0DTE 期权自动交易系统 V4

特性:
- 基于优先级的事件分发
- 同步和异步事件处理
- 类型安全的事件订阅
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Type, TypeVar, Union
from weakref import WeakSet

logger = logging.getLogger(__name__)


class EventPriority(IntEnum):
    """事件优先级 - 数字越小优先级越高"""
    CRITICAL = 0    # 止损、熔断 - 必须最先处理
    HIGH = 10       # 订单状态变更
    NORMAL = 50     # 信号、K线数据
    LOW = 100       # 日志、统计、通知


@dataclass
class Event:
    """事件基类"""
    timestamp: datetime = field(default_factory=datetime.now)
    priority: EventPriority = EventPriority.NORMAL
    
    @property
    def event_type(self) -> str:
        return self.__class__.__name__


EventT = TypeVar('EventT', bound=Event)
SyncHandler = Callable[[Event], None]
AsyncHandler = Callable[[Event], Coroutine[Any, Any, None]]
Handler = Union[SyncHandler, AsyncHandler]


@dataclass
class Subscription:
    """订阅信息"""
    handler: Handler
    priority: EventPriority
    is_async: bool
    filter_func: Optional[Callable[[Event], bool]] = None


class EventBus:
    """
    事件总线 - 组件间解耦通信的核心
    
    特性:
    1. 支持按事件类型订阅
    2. 支持通配符订阅（接收所有事件）
    3. 基于优先级的处理顺序
    4. 同步和异步处理支持
    5. 事件过滤
    
    使用示例:
        bus = EventBus()
        
        # 订阅特定事件
        bus.subscribe(TickEvent, on_tick, priority=EventPriority.CRITICAL)
        
        # 发布事件
        await bus.publish(TickEvent(symbol="SPXW", price=5.0))
        
        # 同步发布（在回调中使用）
        bus.publish_sync(TickEvent(symbol="SPXW", price=5.0))
    """
    
    def __init__(self):
        # 事件类型 -> 订阅列表
        self._subscriptions: Dict[Type[Event], List[Subscription]] = defaultdict(list)
        
        # 通配符订阅（接收所有事件）
        self._wildcard_subscriptions: List[Subscription] = []
        
        # 事件历史（用于调试）
        self._event_history: List[Event] = []
        self._history_max_size: int = 1000
        
        # 统计
        self._event_counts: Dict[str, int] = defaultdict(int)
        
        # 是否启用历史记录
        self._record_history: bool = False
        
        # 异步队列
        self._async_queue: asyncio.Queue = None
        self._processing_task: Optional[asyncio.Task] = None
        
        # 暂停标志
        self._paused: bool = False
    
    def subscribe(
        self,
        event_type: Type[EventT],
        handler: Handler,
        priority: EventPriority = EventPriority.NORMAL,
        filter_func: Optional[Callable[[EventT], bool]] = None
    ) -> None:
        """
        订阅事件
        
        Args:
            event_type: 事件类型
            handler: 处理函数（同步或异步）
            priority: 处理优先级
            filter_func: 可选的过滤函数
        """
        is_async = asyncio.iscoroutinefunction(handler)
        
        subscription = Subscription(
            handler=handler,
            priority=priority,
            is_async=is_async,
            filter_func=filter_func
        )
        
        self._subscriptions[event_type].append(subscription)
        
        # 按优先级排序
        self._subscriptions[event_type].sort(key=lambda s: s.priority)
        
        logger.debug(
            f"Subscribed {handler.__name__} to {event_type.__name__} "
            f"with priority {priority.name}"
        )
    
    def subscribe_all(
        self,
        handler: Handler,
        priority: EventPriority = EventPriority.LOW
    ) -> None:
        """订阅所有事件（通配符）"""
        is_async = asyncio.iscoroutinefunction(handler)
        
        subscription = Subscription(
            handler=handler,
            priority=priority,
            is_async=is_async
        )
        
        self._wildcard_subscriptions.append(subscription)
        self._wildcard_subscriptions.sort(key=lambda s: s.priority)
        
        logger.debug(f"Subscribed {handler.__name__} to ALL events")
    
    def unsubscribe(
        self,
        event_type: Type[Event],
        handler: Handler
    ) -> bool:
        """
        取消订阅
        
        Returns:
            是否成功取消
        """
        subscriptions = self._subscriptions.get(event_type, [])
        
        for i, sub in enumerate(subscriptions):
            if sub.handler == handler:
                subscriptions.pop(i)
                logger.debug(f"Unsubscribed {handler.__name__} from {event_type.__name__}")
                return True
        
        return False
    
    async def publish(self, event: Event) -> None:
        """
        异步发布事件
        
        处理顺序:
        1. 按优先级排序所有订阅者
        2. CRITICAL 优先级的同步执行
        3. 其他优先级并发执行
        """
        if self._paused:
            logger.debug(f"EventBus paused, dropping event: {event.event_type}")
            return
        
        # 记录历史
        if self._record_history:
            self._event_history.append(event)
            if len(self._event_history) > self._history_max_size:
                self._event_history.pop(0)
        
        # 统计
        self._event_counts[event.event_type] += 1
        
        # 获取所有订阅者
        subscriptions = self._get_all_subscriptions(type(event))
        
        if not subscriptions:
            return
        
        # 分离 CRITICAL 和其他订阅
        critical_subs = [s for s in subscriptions if s.priority == EventPriority.CRITICAL]
        other_subs = [s for s in subscriptions if s.priority != EventPriority.CRITICAL]
        
        # CRITICAL 订阅同步执行
        for sub in critical_subs:
            await self._invoke_handler(sub, event)
        
        # 其他订阅并发执行
        if other_subs:
            tasks = [self._invoke_handler(sub, event) for sub in other_subs]
            await asyncio.gather(*tasks, return_exceptions=True)
    
    def publish_sync(self, event: Event) -> None:
        """
        同步发布事件（在回调函数中使用）
        
        注意: 仅执行同步处理器
        """
        if self._paused:
            return
        
        # 记录
        if self._record_history:
            self._event_history.append(event)
            if len(self._event_history) > self._history_max_size:
                self._event_history.pop(0)
        
        self._event_counts[event.event_type] += 1
        
        # 获取订阅
        subscriptions = self._get_all_subscriptions(type(event))
        
        for sub in subscriptions:
            # 只执行同步处理器
            if not sub.is_async:
                try:
                    if sub.filter_func is None or sub.filter_func(event):
                        sub.handler(event)
                except Exception as e:
                    logger.error(f"Error in sync handler {sub.handler.__name__}: {e}")
            else:
                # 对于异步处理器，创建任务
                try:
                    loop = asyncio.get_running_loop()
                    if sub.filter_func is None or sub.filter_func(event):
                        loop.create_task(sub.handler(event))
                except RuntimeError:
                    # 没有运行中的事件循环
                    logger.warning(f"Cannot run async handler {sub.handler.__name__} - no event loop")
    
    async def _invoke_handler(self, sub: Subscription, event: Event) -> None:
        """调用处理器"""
        try:
            # 检查过滤条件
            if sub.filter_func is not None and not sub.filter_func(event):
                return
            
            if sub.is_async:
                await sub.handler(event)
            else:
                sub.handler(event)
                
        except Exception as e:
            logger.error(
                f"Error in event handler {sub.handler.__name__} "
                f"for {event.event_type}: {e}",
                exc_info=True
            )
    
    def _get_all_subscriptions(self, event_type: Type[Event]) -> List[Subscription]:
        """获取事件类型的所有订阅（包括通配符）"""
        subs = list(self._subscriptions.get(event_type, []))
        subs.extend(self._wildcard_subscriptions)
        
        # 按优先级排序
        subs.sort(key=lambda s: s.priority)
        
        return subs
    
    def pause(self) -> None:
        """暂停事件处理"""
        self._paused = True
        logger.info("EventBus paused")
    
    def resume(self) -> None:
        """恢复事件处理"""
        self._paused = False
        logger.info("EventBus resumed")
    
    def enable_history(self, max_size: int = 1000) -> None:
        """启用事件历史记录"""
        self._record_history = True
        self._history_max_size = max_size
    
    def get_history(
        self,
        event_type: Optional[Type[Event]] = None,
        limit: int = 100
    ) -> List[Event]:
        """获取事件历史"""
        events = self._event_history
        
        if event_type:
            events = [e for e in events if isinstance(e, event_type)]
        
        return events[-limit:]
    
    def get_stats(self) -> Dict[str, Any]:
        """获取事件统计"""
        return {
            "total_events": sum(self._event_counts.values()),
            "event_counts": dict(self._event_counts),
            "subscription_counts": {
                k.__name__: len(v) for k, v in self._subscriptions.items()
            },
            "wildcard_subscribers": len(self._wildcard_subscriptions),
            "is_paused": self._paused
        }
    
    def clear_history(self) -> None:
        """清除事件历史"""
        self._event_history.clear()
    
    def clear_stats(self) -> None:
        """清除统计"""
        self._event_counts.clear()


# 全局事件总线实例
_event_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """获取全局事件总线"""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus


def set_event_bus(bus: EventBus) -> None:
    """设置全局事件总线"""
    global _event_bus
    _event_bus = bus
