"""
配置管理模块 - 使用 Pydantic 进行配置验证和管理

SPXW 0DTE 期权自动交易系统 V4
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional
from datetime import time as dt_time

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class SystemConfig(BaseModel):
    """系统配置"""
    mode: Literal["paper", "live", "backtest"] = "paper"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    timezone: str = "America/New_York"


class StorageConfig(BaseModel):
    """存储配置"""
    db_path: str = "data/trading_state.db"
    tick_snapshot_path: str = "data/tick_snapshots/"
    historical_data_path: str = "data/historical/"
    historical_format: Literal["parquet", "csv"] = "parquet"
    tick_buffer_size: int = Field(default=1000, ge=100, le=10000)


class RateLimitConfig(BaseModel):
    """API限流配置"""
    max_requests_per_second: int = Field(default=45, ge=1, le=50)
    max_subscriptions: int = Field(default=100, ge=10, le=200)
    historical_requests_per_10min: int = Field(default=55, ge=1, le=60)


class TickConfig(BaseModel):
    """Tick数据配置"""
    use_tick_by_tick: bool = True
    tick_type: Literal["Last", "AllLast", "BidAsk"] = "Last"
    stale_threshold_seconds: int = Field(default=5, ge=1, le=30)
    stale_action: Literal["alert", "close_position"] = "alert"


class IBKRConfig(BaseModel):
    """IBKR连接配置"""
    host: str = "127.0.0.1"
    port: int = Field(default=7497, ge=1, le=65535)
    client_id: int = Field(default=1, ge=1, le=32)
    timeout: int = Field(default=30, ge=5, le=120)
    reconnect_attempts: int = Field(default=5, ge=1, le=10)
    reconnect_delays: List[int] = [1, 2, 4, 8, 16]
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    tick: TickConfig = Field(default_factory=TickConfig)


class InstrumentConfig(BaseModel):
    """交易标的配置"""
    underlying: str = "SPX"
    option_type: str = "SPXW"
    exchange: str = "SMART"
    currency: str = "USD"


class OptionPoolConfig(BaseModel):
    """期权池配置"""
    enabled: bool = True
    strikes_around_atm: int = Field(default=2, ge=1, le=5)
    refresh_price_threshold: float = Field(default=5.0, ge=1.0, le=20.0)
    refresh_interval_seconds: int = Field(default=60, ge=10, le=300)


class StrategyConfig(BaseModel):
    """策略参数配置"""
    bar_size: str = "5 mins"
    trend_bar_size: str = "1 hour"
    
    # 历史数据预热
    warmup_bars: int = Field(default=120, ge=20, le=500)
    warmup_trend_bars: int = Field(default=48, ge=10, le=200)
    
    # 通道参数
    channel_lookback: int = Field(default=20, ge=1, le=100)
    channel_type: Literal["body", "wick"] = "body"
    
    # 趋势参数
    ema_period: int = Field(default=20, ge=5, le=100)
    slope_bull_threshold: float = Field(default=0.0005, ge=0.0, le=0.01)
    slope_bear_threshold: float = Field(default=-0.0005, ge=-0.01, le=0.0)
    
    # 信号过滤
    cooldown_bars: int = Field(default=3, ge=1, le=10)
    signal_lock_seconds: int = Field(default=60, ge=10, le=600)
    max_concurrent_positions: int = Field(default=3, ge=1, le=10)


class LiquidityConfig(BaseModel):
    """流动性检查配置"""
    max_bid_ask_spread: float = Field(default=0.20, ge=0.05, le=1.0)
    max_spread_pct: float = Field(default=0.05, ge=0.01, le=0.20)
    min_bid_size: int = Field(default=1, ge=1, le=100)  # 放宽：0DTE 期权挂单量波动大
    min_ask_size: int = Field(default=1, ge=1, le=100)  # 放宽：0DTE 期权挂单量波动大
    reject_wide_spread: bool = True


class OptionSelectionConfig(BaseModel):
    """期权选择配置"""
    strike_offset: int = Field(default=0, ge=-10, le=10)
    strike_interval: int = Field(default=5, ge=1, le=25)
    prefer_otm: bool = True
    min_delta: float = Field(default=0.30, ge=0.10, le=0.60)
    max_delta: float = Field(default=0.50, ge=0.30, le=0.70)
    min_open_interest: int = Field(default=100, ge=0, le=10000)
    min_bid: float = Field(default=0.50, ge=0.05, le=5.0)
    liquidity: LiquidityConfig = Field(default_factory=LiquidityConfig)
    
    @model_validator(mode='after')
    def validate_delta_range(self) -> 'OptionSelectionConfig':
        if self.min_delta >= self.max_delta:
            raise ValueError("min_delta must be less than max_delta")
        return self


class ExecutionConfig(BaseModel):
    """订单执行配置"""
    order_type: Literal["LMT", "MKT"] = "LMT"
    limit_price_buffer: float = Field(default=0.05, ge=0.01, le=0.50)
    order_timeout: int = Field(default=30, ge=5, le=120)
    max_slippage: float = Field(default=0.10, ge=0.01, le=0.50)
    position_size: int = Field(default=1, ge=1, le=100)
    max_position: int = Field(default=5, ge=1, le=50)
    max_premium_per_trade: float = Field(default=500.0, ge=50.0, le=10000.0)
    enforce_long_only: bool = True  # CRITICAL: 强制买方策略
    
    # 仓位计算：固定投入金额
    # 如果 > 0，则根据期权价格计算手数：手数 = floor(金额 / (价格 * 100))
    # 不到1手按1手计算
    # 如果 = 0，则使用固定的 position_size
    fixed_investment_amount: float = Field(default=0.0, ge=0.0, le=100000.0)


class TickFilterConfig(BaseModel):
    """Tick过滤配置"""
    enabled: bool = True
    mid_deviation_threshold: float = Field(default=0.10, ge=0.05, le=0.30)
    jump_threshold: float = Field(default=0.20, ge=0.10, le=0.50)
    confirm_ticks: int = Field(default=2, ge=1, le=5)
    confirm_duration_ms: int = Field(default=50, ge=10, le=500)


class ChaseStopConfig(BaseModel):
    """动态追单止损配置 - V4核心功能"""
    # Phase 1: 激进限价
    initial_buffer: float = Field(default=0.10, ge=0.01, le=0.50)
    
    # Phase 2: 动态追单
    chase_interval_ms: int = Field(default=500, ge=100, le=2000)
    chase_buffer: float = Field(default=0.10, ge=0.01, le=0.50)
    max_chase_count: int = Field(default=3, ge=1, le=10)
    max_chase_duration_ms: int = Field(default=2000, ge=500, le=10000)
    
    # Phase 3: 紧急抛售
    panic_loss_threshold: float = Field(default=0.40, ge=0.20, le=0.80)
    panic_price_factor: float = Field(default=0.95, ge=0.80, le=0.99)
    abandon_price_threshold: float = Field(default=0.20, ge=0.05, le=1.0)
    
    # 最后手段
    enable_market_fallback: bool = True
    market_fallback_delay_ms: int = Field(default=1000, ge=500, le=5000)


class RiskConfig(BaseModel):
    """风险管理配置"""
    # 止损阈值
    hard_stop_pct: float = Field(default=0.20, ge=0.05, le=0.50)
    breakeven_trigger_pct: float = Field(default=0.15, ge=0.05, le=0.50)
    trailing_activation_pct: float = Field(default=0.30, ge=0.10, le=1.0)
    trailing_stop_pct: float = Field(default=0.10, ge=0.05, le=0.30)
    
    # Tick过滤
    tick_filter: TickFilterConfig = Field(default_factory=TickFilterConfig)
    
    # 动态追单止损
    chase_stop: ChaseStopConfig = Field(default_factory=ChaseStopConfig)
    
    # 熔断器
    daily_loss_limit: float = Field(default=1000.0, ge=100.0, le=50000.0)
    max_daily_trades: int = Field(default=20, ge=1, le=100)
    circuit_breaker_cooldown: int = Field(default=3600, ge=300, le=86400)
    
    # 时间控制
    market_close_buffer_minutes: int = Field(default=10, ge=5, le=60)
    trading_start_buffer_minutes: int = Field(default=5, ge=0, le=30)


class PerformanceConfig(BaseModel):
    """绩效追踪配置"""
    snapshot_interval: int = Field(default=60, ge=10, le=600)
    daily_summary_time: str = "16:30"
    rolling_window_days: int = Field(default=30, ge=7, le=365)
    benchmark: str = "SPY"
    auto_export_daily: bool = True
    export_format: Literal["csv", "json", "parquet"] = "csv"
    export_path: str = "reports/"


class TelegramConfig(BaseModel):
    """Telegram通知配置"""
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""
    parse_mode: Literal["HTML", "Markdown"] = "HTML"
    rate_limit: int = Field(default=30, ge=1, le=60)
    
    @field_validator('bot_token', 'chat_id')
    @classmethod
    def expand_env_vars(cls, v: str) -> str:
        """展开环境变量"""
        if v.startswith("${") and v.endswith("}"):
            env_var = v[2:-1]
            return os.environ.get(env_var, "")
        return v


class EmailConfig(BaseModel):
    """邮件通知配置"""
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = Field(default=587, ge=1, le=65535)
    use_tls: bool = True
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: List[str] = []
    
    @field_validator('username', 'password')
    @classmethod
    def expand_env_vars(cls, v: str) -> str:
        """展开环境变量"""
        if v.startswith("${") and v.endswith("}"):
            env_var = v[2:-1]
            return os.environ.get(env_var, "")
        return v


class NotificationTriggersConfig(BaseModel):
    """通知触发条件配置"""
    on_trade_open: bool = True
    on_trade_close: bool = True
    on_stop_loss: bool = True
    on_stop_abandoned: bool = True
    on_circuit_breaker: bool = True
    on_error: bool = True
    on_pacing_violation: bool = True
    on_daily_summary: bool = True
    pnl_threshold: float = Field(default=500.0, ge=0.0)


class NotificationConfig(BaseModel):
    """通知配置"""
    enabled: bool = True
    triggers: NotificationTriggersConfig = Field(default_factory=NotificationTriggersConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)


class BacktestConfig(BaseModel):
    """回测配置"""
    data_source: Literal["parquet", "csv", "ibkr"] = "parquet"
    data_path: str = "data/historical/"
    start_date: str = "2024-01-01"
    end_date: str = "2024-12-31"
    initial_capital: float = Field(default=100000.0, ge=10000.0)
    commission_per_contract: float = Field(default=0.65, ge=0.0, le=10.0)
    slippage_model: Literal["fixed", "percent", "tick"] = "fixed"
    slippage_value: float = Field(default=0.05, ge=0.0, le=1.0)
    simulate_ticks_from_bars: bool = True
    tick_simulation_points: int = Field(default=4, ge=2, le=10)
    save_trades: bool = True
    save_equity_curve: bool = True
    generate_report: bool = True


class UIConfig(BaseModel):
    """UI配置"""
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1024, le=65535)
    refresh_rate: int = Field(default=1000, ge=100, le=10000)
    theme: Literal["dark", "light"] = "dark"


class TradingConfig(BaseModel):
    """主配置类 - 聚合所有配置"""
    system: SystemConfig = Field(default_factory=SystemConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    ibkr: IBKRConfig = Field(default_factory=IBKRConfig)
    instrument: InstrumentConfig = Field(default_factory=InstrumentConfig)
    option_pool: OptionPoolConfig = Field(default_factory=OptionPoolConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    option_selection: OptionSelectionConfig = Field(default_factory=OptionSelectionConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    notification: NotificationConfig = Field(default_factory=NotificationConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    
    @classmethod
    def from_yaml(cls, path: str | Path) -> 'TradingConfig':
        """从YAML文件加载配置"""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        
        with open(path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        
        return cls(**data)
    
    def to_yaml(self, path: str | Path) -> None:
        """保存配置到YAML文件"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, 'w', encoding='utf-8') as f:
            yaml.dump(self.model_dump(), f, default_flow_style=False, allow_unicode=True)
    
    def validate_for_mode(self) -> None:
        """根据运行模式验证配置"""
        if self.system.mode == "live":
            # 实盘模式额外检查
            if not self.execution.enforce_long_only:
                raise ValueError("CRITICAL: enforce_long_only must be True in live mode!")
            
            if self.notification.telegram.enabled:
                if not self.notification.telegram.bot_token or not self.notification.telegram.chat_id:
                    raise ValueError("Telegram credentials required in live mode")


def load_config(config_path: Optional[str] = None) -> TradingConfig:
    """
    加载配置的便捷函数
    
    优先级:
    1. 指定路径
    2. 环境变量 TRADING_CONFIG_PATH
    3. 默认路径 config/settings.yaml
    """
    if config_path is None:
        config_path = os.environ.get('TRADING_CONFIG_PATH', 'config/settings.yaml')
    
    return TradingConfig.from_yaml(config_path)


# 配置单例
_config: Optional[TradingConfig] = None


def get_config() -> TradingConfig:
    """获取全局配置单例"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def set_config(config: TradingConfig) -> None:
    """设置全局配置单例"""
    global _config
    _config = config
