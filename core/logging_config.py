"""
日志配置模块 - 支持按日期生成日志文件

SPXW 0DTE 期权自动交易系统 V4

特性:
- 控制台输出 + 文件日志
- 按日期自动分割日志文件
- 支持不同级别的日志
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def setup_logging(
    log_dir: str = "logs",
    log_level: int = logging.INFO,
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG
) -> None:
    """
    配置日志系统
    
    Args:
        log_dir: 日志目录
        log_level: 根日志级别
        console_level: 控制台日志级别
        file_level: 文件日志级别
    """
    # 确保日志目录存在
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    
    # 日志格式
    console_format = '%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s'
    file_format = '%(asctime)s | %(levelname)-8s | %(name)-25s | %(funcName)-20s | %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    
    # 获取根 logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    
    # 清除已有的 handlers（避免重复）
    root_logger.handlers.clear()
    
    # 控制台 Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(console_level)
    console_handler.setFormatter(logging.Formatter(console_format, date_format))
    root_logger.addHandler(console_handler)
    
    # 文件 Handler - 按日期分割
    today = datetime.now().strftime('%Y-%m-%d')
    log_file = log_path / f"trading_{today}.log"
    
    file_handler = TimedRotatingFileHandler(
        filename=str(log_file),
        when='midnight',
        interval=1,
        backupCount=30,  # 保留 30 天
        encoding='utf-8'
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(logging.Formatter(file_format, date_format))
    file_handler.suffix = "%Y-%m-%d"  # 备份文件后缀格式
    root_logger.addHandler(file_handler)
    
    # 交易日志 - 单独记录交易相关信息
    trade_log_file = log_path / f"trades_{today}.log"
    trade_handler = TimedRotatingFileHandler(
        filename=str(trade_log_file),
        when='midnight',
        interval=1,
        backupCount=90,  # 保留 90 天
        encoding='utf-8'
    )
    trade_handler.setLevel(logging.INFO)
    trade_handler.setFormatter(logging.Formatter(file_format, date_format))
    trade_handler.suffix = "%Y-%m-%d"
    
    # 为交易相关的 logger 添加专门的 handler
    trade_loggers = [
        'execution.order_manager',
        'risk.stop_manager',
        'risk.chase_stop_executor',
        'strategy.channel_breakout'
    ]
    for logger_name in trade_loggers:
        trade_logger = logging.getLogger(logger_name)
        trade_logger.addHandler(trade_handler)
    
    # 错误日志 - 单独记录错误
    error_log_file = log_path / f"errors_{today}.log"
    error_handler = TimedRotatingFileHandler(
        filename=str(error_log_file),
        when='midnight',
        interval=1,
        backupCount=30,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(logging.Formatter(file_format, date_format))
    error_handler.suffix = "%Y-%m-%d"
    root_logger.addHandler(error_handler)
    
    # 降低第三方库的日志级别
    logging.getLogger('ib_insync').setLevel(logging.INFO)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logging.getLogger('aiosqlite').setLevel(logging.WARNING)
    logging.getLogger('nicegui').setLevel(logging.WARNING)
    logging.getLogger('uvicorn').setLevel(logging.WARNING)
    logging.getLogger('uvicorn.access').setLevel(logging.WARNING)
    
    logging.info(f"Logging initialized: {log_file}")
    logging.info(f"Trade log: {trade_log_file}")
    logging.info(f"Error log: {error_log_file}")


def get_logger(name: str) -> logging.Logger:
    """获取 logger"""
    return logging.getLogger(name)
