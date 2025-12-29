"""
邮件通知模块

SPXW 0DTE 期权自动交易系统 V4

使用 aiosmtplib 异步发送邮件
"""

from __future__ import annotations

import asyncio
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Optional
from pathlib import Path

import aiosmtplib

from .dispatcher import NotificationChannel, Notification, NotificationPriority
from core.config import EmailConfig

logger = logging.getLogger(__name__)


class EmailNotifier(NotificationChannel):
    """
    邮件通知器
    
    使用异步 SMTP 发送邮件
    """
    
    def __init__(self, config: EmailConfig):
        self.config = config
        self.enabled = config.enabled
        
        # SMTP 配置
        self.smtp_host = config.smtp_host
        self.smtp_port = config.smtp_port
        self.username = config.username
        self.password = config.password
        self.sender = config.sender
        self.recipients = config.recipients
        self.use_tls = config.use_tls
        
        # 速率限制（避免发送过多邮件）
        self._last_send_time: float = 0
        self._min_interval: float = 60.0  # 最小发送间隔（秒）
        
        # 邮件队列（用于批量发送）
        self._pending_notifications: List[Notification] = []
        self._batch_interval: float = 300.0  # 批量发送间隔（5分钟）
        
        logger.info(f"EmailNotifier initialized: enabled={self.enabled}")
    
    def is_enabled(self) -> bool:
        """是否启用"""
        return (
            self.enabled and
            bool(self.smtp_host) and
            bool(self.username) and
            bool(self.recipients)
        )
    
    async def send(self, notification: Notification) -> bool:
        """发送通知"""
        if not self.is_enabled():
            return False
        
        # 只发送高优先级和紧急通知
        if notification.priority not in [NotificationPriority.HIGH, NotificationPriority.URGENT]:
            # 低优先级通知加入队列，批量发送
            self._pending_notifications.append(notification)
            return True
        
        try:
            # 高优先级立即发送
            subject = self._format_subject(notification)
            body = self._format_body(notification)
            
            success = await self._send_email(
                subject=subject,
                body=body,
                is_html=False
            )
            
            if success:
                logger.debug(f"Email notification sent: {notification.title}")
            
            return success
            
        except Exception as e:
            logger.error(f"Failed to send email notification: {e}")
            return False
    
    def _format_subject(self, notification: Notification) -> str:
        """格式化邮件主题"""
        prefix = "[SPXW Trading]"
        
        priority_tag = {
            NotificationPriority.URGENT: "[URGENT]",
            NotificationPriority.HIGH: "[IMPORTANT]",
            NotificationPriority.NORMAL: "",
            NotificationPriority.LOW: ""
        }
        
        tag = priority_tag.get(notification.priority, "")
        
        return f"{prefix} {tag} {notification.title}".strip()
    
    def _format_body(self, notification: Notification) -> str:
        """格式化邮件正文"""
        lines = [
            f"Time: {notification.timestamp.strftime('%Y-%m-%d %H:%M:%S')}",
            f"Type: {notification.notification_type}",
            f"Priority: {notification.priority.value}",
            "",
            "=" * 50,
            "",
            notification.message,
            "",
            "=" * 50,
            "",
            "This is an automated message from SPXW Trading System.",
            "Do not reply to this email."
        ]
        
        return "\n".join(lines)
    
    async def _send_email(
        self,
        subject: str,
        body: str,
        is_html: bool = False,
        attachments: Optional[List[str]] = None
    ) -> bool:
        """发送邮件"""
        # 速率限制检查
        import time
        now = time.time()
        if now - self._last_send_time < self._min_interval:
            logger.debug("Email rate limited, skipping")
            return True
        
        try:
            # 创建邮件
            msg = MIMEMultipart()
            msg['From'] = self.sender
            msg['To'] = ', '.join(self.recipients)
            msg['Subject'] = subject
            
            # 添加正文
            content_type = 'html' if is_html else 'plain'
            msg.attach(MIMEText(body, content_type))
            
            # 添加附件
            if attachments:
                for file_path in attachments:
                    await self._attach_file(msg, file_path)
            
            # 发送
            await aiosmtplib.send(
                msg,
                hostname=self.smtp_host,
                port=self.smtp_port,
                username=self.username,
                password=self.password,
                start_tls=self.use_tls
            )
            
            self._last_send_time = time.time()
            return True
            
        except Exception as e:
            logger.error(f"SMTP error: {e}")
            return False
    
    async def _attach_file(self, msg: MIMEMultipart, file_path: str) -> None:
        """添加附件"""
        path = Path(file_path)
        
        if not path.exists():
            logger.warning(f"Attachment not found: {file_path}")
            return
        
        with open(path, 'rb') as f:
            part = MIMEBase('application', 'octet-stream')
            part.set_payload(f.read())
        
        encoders.encode_base64(part)
        part.add_header(
            'Content-Disposition',
            f'attachment; filename="{path.name}"'
        )
        
        msg.attach(part)
    
    async def send_daily_report(
        self,
        report_data: dict,
        chart_path: Optional[str] = None
    ) -> bool:
        """发送每日报告"""
        if not self.is_enabled():
            return False
        
        subject = f"[SPXW Trading] Daily Report - {report_data.get('date', 'Today')}"
        
        # 构建 HTML 报告
        body = self._build_daily_report_html(report_data)
        
        attachments = [chart_path] if chart_path else None
        
        return await self._send_email(
            subject=subject,
            body=body,
            is_html=True,
            attachments=attachments
        )
    
    def _build_daily_report_html(self, data: dict) -> str:
        """构建每日报告 HTML"""
        return f"""
        <html>
        <head>
            <style>
                body {{ font-family: Arial, sans-serif; }}
                table {{ border-collapse: collapse; width: 100%; }}
                th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
                th {{ background-color: #4CAF50; color: white; }}
                .positive {{ color: green; }}
                .negative {{ color: red; }}
            </style>
        </head>
        <body>
            <h2>SPXW Trading Daily Report</h2>
            <p>Date: {data.get('date', 'N/A')}</p>
            
            <h3>Summary</h3>
            <table>
                <tr><th>Metric</th><th>Value</th></tr>
                <tr><td>Total Trades</td><td>{data.get('trades', 0)}</td></tr>
                <tr><td>Wins / Losses</td><td>{data.get('wins', 0)} / {data.get('losses', 0)}</td></tr>
                <tr><td>Win Rate</td><td>{data.get('win_rate', '0%')}</td></tr>
                <tr><td>Total P&L</td><td class="{'positive' if float(data.get('total_pnl', '$0').replace('$', '')) >= 0 else 'negative'}">{data.get('total_pnl', '$0')}</td></tr>
                <tr><td>Profit Factor</td><td>{data.get('profit_factor', '0')}</td></tr>
            </table>
            
            <p style="color: gray; font-size: 12px;">
                This is an automated report from SPXW Trading System.
            </p>
        </body>
        </html>
        """
    
    async def send_batch_notifications(self) -> bool:
        """发送批量通知"""
        if not self._pending_notifications:
            return True
        
        notifications = self._pending_notifications.copy()
        self._pending_notifications.clear()
        
        # 合并通知
        subject = f"[SPXW Trading] {len(notifications)} Notifications Summary"
        
        body_parts = []
        for n in notifications:
            body_parts.append(
                f"[{n.timestamp.strftime('%H:%M:%S')}] {n.title}\n{n.message}\n"
            )
        
        body = "\n".join(["=" * 50, ""] + body_parts)
        
        return await self._send_email(subject=subject, body=body)
    
    async def test_connection(self) -> bool:
        """测试连接"""
        if not self.is_enabled():
            return False
        
        try:
            async with aiosmtplib.SMTP(
                hostname=self.smtp_host,
                port=self.smtp_port,
                start_tls=self.use_tls
            ) as smtp:
                await smtp.login(self.username, self.password)
                logger.info("Email SMTP connection test passed")
                return True
        except Exception as e:
            logger.error(f"Email connection test failed: {e}")
            return False
