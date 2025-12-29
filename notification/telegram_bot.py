"""
Telegram 通知模块

SPXW 0DTE 期权自动交易系统 V4

使用 aiohttp 异步发送 Telegram 消息
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from .dispatcher import NotificationChannel, Notification, NotificationPriority
from core.config import TelegramConfig

logger = logging.getLogger(__name__)


class TelegramNotifier(NotificationChannel):
    """
    Telegram 通知器
    
    使用 Telegram Bot API 发送消息
    """
    
    def __init__(self, config: TelegramConfig):
        self.config = config
        self.bot_token = config.bot_token
        self.chat_id = config.chat_id
        self.enabled = config.enabled
        
        # API URL
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        
        # HTTP 会话
        self._session: Optional[aiohttp.ClientSession] = None
        
        # 速率限制
        self._last_send_time: float = 0
        self._min_interval: float = 0.5  # 最小发送间隔（秒）
        
        logger.info(f"TelegramNotifier initialized: enabled={self.enabled}")
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """获取 HTTP 会话"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session
    
    async def close(self) -> None:
        """关闭会话"""
        if self._session and not self._session.closed:
            await self._session.close()
    
    def is_enabled(self) -> bool:
        """是否启用"""
        return self.enabled and bool(self.bot_token) and bool(self.chat_id)
    
    async def send(self, notification: Notification) -> bool:
        """发送通知"""
        if not self.is_enabled():
            return False
        
        try:
            # 格式化消息
            text = self._format_message(notification)
            
            # 发送
            success = await self._send_message(text)
            
            if success:
                logger.debug(f"Telegram notification sent: {notification.title}")
            
            return success
            
        except Exception as e:
            logger.error(f"Failed to send Telegram notification: {e}")
            return False
    
    def _format_message(self, notification: Notification) -> str:
        """格式化消息"""
        # 添加优先级标识
        priority_emoji = {
            NotificationPriority.LOW: "ℹ️",
            NotificationPriority.NORMAL: "📌",
            NotificationPriority.HIGH: "⚠️",
            NotificationPriority.URGENT: "🚨"
        }
        
        emoji = priority_emoji.get(notification.priority, "📌")
        
        # 构建消息
        lines = [
            f"{emoji} *{notification.title}*",
            "",
            notification.message,
            "",
            f"_{notification.timestamp.strftime('%Y-%m-%d %H:%M:%S')}_"
        ]
        
        return "\n".join(lines)
    
    async def _send_message(
        self,
        text: str,
        parse_mode: str = "Markdown"
    ) -> bool:
        """发送消息到 Telegram"""
        # 速率限制
        import time
        now = time.time()
        if now - self._last_send_time < self._min_interval:
            await asyncio.sleep(self._min_interval - (now - self._last_send_time))
        
        session = await self._get_session()
        
        url = f"{self.api_base}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        
        try:
            async with session.post(url, json=payload) as response:
                self._last_send_time = time.time()
                
                if response.status == 200:
                    return True
                else:
                    error = await response.text()
                    logger.error(f"Telegram API error: {response.status} - {error}")
                    return False
                    
        except aiohttp.ClientError as e:
            logger.error(f"Telegram request failed: {e}")
            return False
    
    async def send_photo(
        self,
        photo_path: str,
        caption: str = ""
    ) -> bool:
        """发送图片"""
        if not self.is_enabled():
            return False
        
        session = await self._get_session()
        
        url = f"{self.api_base}/sendPhoto"
        
        try:
            with open(photo_path, 'rb') as photo:
                data = aiohttp.FormData()
                data.add_field('chat_id', self.chat_id)
                data.add_field('photo', photo)
                if caption:
                    data.add_field('caption', caption)
                
                async with session.post(url, data=data) as response:
                    return response.status == 200
                    
        except Exception as e:
            logger.error(f"Failed to send photo: {e}")
            return False
    
    async def send_document(
        self,
        document_path: str,
        caption: str = ""
    ) -> bool:
        """发送文档"""
        if not self.is_enabled():
            return False
        
        session = await self._get_session()
        
        url = f"{self.api_base}/sendDocument"
        
        try:
            with open(document_path, 'rb') as doc:
                data = aiohttp.FormData()
                data.add_field('chat_id', self.chat_id)
                data.add_field('document', doc)
                if caption:
                    data.add_field('caption', caption)
                
                async with session.post(url, data=data) as response:
                    return response.status == 200
                    
        except Exception as e:
            logger.error(f"Failed to send document: {e}")
            return False
    
    async def test_connection(self) -> bool:
        """测试连接"""
        if not self.bot_token:
            return False
        
        session = await self._get_session()
        url = f"{self.api_base}/getMe"
        
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    bot_name = data.get("result", {}).get("username", "Unknown")
                    logger.info(f"Telegram bot connected: @{bot_name}")
                    return True
                return False
        except Exception as e:
            logger.error(f"Telegram connection test failed: {e}")
            return False
