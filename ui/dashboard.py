"""
Web UI 仪表盘 - NiceGUI 实现

SPXW 0DTE 期权自动交易系统 V4

特性:
- 深色主题
- 实时数据更新
- 与主程序集成运行
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Optional, Dict, Any, List

from nicegui import ui, app

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class TradingDashboard:
    """交易仪表盘"""
    
    def __init__(
        self,
        trading_system: Optional[Any] = None,
        host: str = "0.0.0.0",
        port: int = 8080
    ):
        self.system = trading_system
        self.host = host
        self.port = port
        
        # UI 元素引用
        self._status_label: Optional[ui.label] = None
        self._pnl_label: Optional[ui.label] = None
        self._positions_table: Optional[ui.table] = None
        self._trades_table: Optional[ui.table] = None
        self._equity_chart: Optional[ui.echart] = None
        self._log_area: Optional[ui.log] = None
        self._positions_count_label: Optional[ui.label] = None
        self._trades_count_label: Optional[ui.label] = None
        
        # 策略状态标签
        self._spx_price_label: Optional[ui.label] = None
        self._channel_label: Optional[ui.label] = None
        self._trend_label: Optional[ui.label] = None
        self._signals_count_label: Optional[ui.label] = None
        
        self._setup_pages()
        logger.info(f"Dashboard initialized: http://{host}:{port}")
    
    def _setup_pages(self) -> None:
        """设置页面路由"""
        
        @ui.page('/')
        async def main_page():
            await self._create_main_page()
        
        @ui.page('/positions')
        async def positions_page():
            await self._create_positions_page()
        
        @ui.page('/trades')
        async def trades_page():
            await self._create_trades_page()
        
        @ui.page('/performance')
        async def performance_page():
            await self._create_performance_page()
        
        @ui.page('/settings')
        async def settings_page():
            await self._create_settings_page()
    
    async def _create_header(self, title: str = "SPXW 0DTE Trading System") -> None:
        """创建页面头部"""
        ui.dark_mode().enable()
        
        with ui.header().classes('bg-gray-900 text-white'):
            with ui.row().classes('w-full items-center'):
                ui.label(title).classes('text-xl font-bold')
                ui.space()
                
                ui.button('Dashboard', on_click=lambda: ui.navigate.to('/')).props('flat color=white')
                ui.button('Positions', on_click=lambda: ui.navigate.to('/positions')).props('flat color=white')
                ui.button('Trades', on_click=lambda: ui.navigate.to('/trades')).props('flat color=white')
                ui.button('Performance', on_click=lambda: ui.navigate.to('/performance')).props('flat color=white')
                ui.button('Settings', on_click=lambda: ui.navigate.to('/settings')).props('flat color=white')
    
    async def _create_main_page(self) -> None:
        """主仪表盘页面"""
        await self._create_header()
        
        with ui.column().classes('w-full p-4 gap-4'):
            # 状态卡片行
            with ui.row().classes('w-full gap-4'):
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('System Status').classes('text-gray-400 text-sm')
                    self._status_label = ui.label('INITIALIZING').classes('text-2xl font-bold text-yellow-400')
                
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('Today P&L').classes('text-gray-400 text-sm')
                    self._pnl_label = ui.label('$0.00').classes('text-2xl font-bold text-white')
                
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('Open Positions').classes('text-gray-400 text-sm')
                    self._positions_count_label = ui.label('0').classes('text-2xl font-bold text-white')
                
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('Today Trades').classes('text-gray-400 text-sm')
                    self._trades_count_label = ui.label('0').classes('text-2xl font-bold text-white')
            
            # 策略状态行 (通道、趋势、SPX 价格)
            with ui.row().classes('w-full gap-4'):
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('SPX Price').classes('text-gray-400 text-sm')
                    self._spx_price_label = ui.label('$0.00').classes('text-2xl font-bold text-white')
                
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('Channel').classes('text-gray-400 text-sm')
                    self._channel_label = ui.label('[ - , - ]').classes('text-lg font-mono text-cyan-400')
                
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('Trend').classes('text-gray-400 text-sm')
                    self._trend_label = ui.label('NEUTRAL').classes('text-2xl font-bold text-gray-400')
                
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('Signals Today').classes('text-gray-400 text-sm')
                    self._signals_count_label = ui.label('0').classes('text-2xl font-bold text-white')
            
            # 图表和持仓
            with ui.row().classes('w-full gap-4'):
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('Equity Curve').classes('text-gray-400 text-sm mb-2')
                    self._equity_chart = ui.echart({
                        'backgroundColor': 'transparent',
                        'xAxis': {'type': 'category', 'data': [], 'axisLine': {'lineStyle': {'color': '#666'}}},
                        'yAxis': {'type': 'value', 'axisLine': {'lineStyle': {'color': '#666'}}, 'splitLine': {'lineStyle': {'color': '#333'}}},
                        'series': [{'type': 'line', 'data': [], 'smooth': True, 'areaStyle': {'opacity': 0.3}, 'lineStyle': {'color': '#4ade80'}, 'itemStyle': {'color': '#4ade80'}}],
                        'tooltip': {'trigger': 'axis'},
                        'grid': {'left': '10%', 'right': '5%', 'top': '10%', 'bottom': '15%'}
                    }).classes('w-full h-64')
                
                with ui.card().classes('bg-gray-800 flex-1'):
                    ui.label('Open Positions').classes('text-gray-400 text-sm mb-2')
                    self._positions_table = ui.table(
                        columns=[
                            {'name': 'contract', 'label': 'Contract', 'field': 'contract', 'align': 'left'},
                            {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'align': 'center'},
                            {'name': 'entry', 'label': 'Entry', 'field': 'entry', 'align': 'right'},
                            {'name': 'current', 'label': 'Current', 'field': 'current', 'align': 'right'},
                            {'name': 'pnl', 'label': 'P&L', 'field': 'pnl', 'align': 'right'},
                            {'name': 'stop', 'label': 'Stop', 'field': 'stop', 'align': 'center'},
                        ],
                        rows=[],
                        row_key='contract'
                    ).classes('w-full').props('dark dense')
            
            # 最近交易
            with ui.card().classes('bg-gray-800 w-full'):
                ui.label('Recent Trades').classes('text-gray-400 text-sm mb-2')
                self._trades_table = ui.table(
                    columns=[
                        {'name': 'time', 'label': 'Time', 'field': 'time', 'align': 'left'},
                        {'name': 'contract', 'label': 'Contract', 'field': 'contract', 'align': 'left'},
                        {'name': 'action', 'label': 'Action', 'field': 'action', 'align': 'center'},
                        {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'align': 'center'},
                        {'name': 'price', 'label': 'Price', 'field': 'price', 'align': 'right'},
                        {'name': 'pnl', 'label': 'P&L', 'field': 'pnl', 'align': 'right'},
                    ],
                    rows=[],
                    row_key='time'
                ).classes('w-full').props('dark dense')
            
            # 日志区域
            with ui.card().classes('bg-gray-800 w-full'):
                ui.label('System Log').classes('text-gray-400 text-sm mb-2')
                self._log_area = ui.log(max_lines=20).classes('w-full h-32 bg-gray-900 text-green-400 font-mono text-sm')
        
        ui.timer(1.0, self._update_dashboard_data)
    
    async def _create_positions_page(self) -> None:
        """持仓详情页面"""
        await self._create_header("Positions")
        
        with ui.column().classes('w-full p-4 gap-4'):
            with ui.card().classes('bg-gray-800 w-full'):
                ui.label('All Open Positions').classes('text-xl text-white mb-4')
                
                ui.table(
                    columns=[
                        {'name': 'contract', 'label': 'Contract', 'field': 'contract', 'align': 'left'},
                        {'name': 'qty', 'label': 'Quantity', 'field': 'qty', 'align': 'center'},
                        {'name': 'entry_price', 'label': 'Entry Price', 'field': 'entry_price', 'align': 'right'},
                        {'name': 'current_price', 'label': 'Current', 'field': 'current_price', 'align': 'right'},
                        {'name': 'highest_price', 'label': 'Highest', 'field': 'highest_price', 'align': 'right'},
                        {'name': 'unrealized_pnl', 'label': 'Unrealized P&L', 'field': 'unrealized_pnl', 'align': 'right'},
                        {'name': 'stop_type', 'label': 'Stop Type', 'field': 'stop_type', 'align': 'center'},
                        {'name': 'entry_time', 'label': 'Entry Time', 'field': 'entry_time', 'align': 'left'},
                    ],
                    rows=[],
                    row_key='contract'
                ).classes('w-full').props('dark')
    
    async def _create_trades_page(self) -> None:
        """交易历史页面"""
        await self._create_header("Trade History")
        
        with ui.column().classes('w-full p-4 gap-4'):
            with ui.card().classes('bg-gray-800 w-full'):
                ui.label('Trade History').classes('text-xl text-white mb-4')
                
                ui.table(
                    columns=[
                        {'name': 'entry_time', 'label': 'Entry Time', 'field': 'entry_time', 'align': 'left'},
                        {'name': 'exit_time', 'label': 'Exit Time', 'field': 'exit_time', 'align': 'left'},
                        {'name': 'contract', 'label': 'Contract', 'field': 'contract', 'align': 'left'},
                        {'name': 'direction', 'label': 'Direction', 'field': 'direction', 'align': 'center'},
                        {'name': 'qty', 'label': 'Qty', 'field': 'qty', 'align': 'center'},
                        {'name': 'entry_price', 'label': 'Entry', 'field': 'entry_price', 'align': 'right'},
                        {'name': 'exit_price', 'label': 'Exit', 'field': 'exit_price', 'align': 'right'},
                        {'name': 'pnl', 'label': 'P&L', 'field': 'pnl', 'align': 'right'},
                        {'name': 'stop_type', 'label': 'Stop Type', 'field': 'stop_type', 'align': 'center'},
                    ],
                    rows=[],
                    row_key='entry_time',
                    pagination={'rowsPerPage': 20}
                ).classes('w-full').props('dark')
    
    async def _create_performance_page(self) -> None:
        """绩效分析页面"""
        await self._create_header("Performance")
        
        with ui.column().classes('w-full p-4 gap-4'):
            with ui.row().classes('w-full gap-4'):
                metrics = [
                    ('Total P&L', '$0.00', 'text-white'),
                    ('Win Rate', '0%', 'text-white'),
                    ('Profit Factor', '0.00', 'text-white'),
                    ('Sharpe Ratio', '0.00', 'text-white'),
                    ('Max Drawdown', '$0.00', 'text-red-400'),
                    ('Avg Win', '$0.00', 'text-green-400'),
                    ('Avg Loss', '$0.00', 'text-red-400'),
                    ('Total Trades', '0', 'text-white'),
                ]
                
                for label, value, color in metrics:
                    with ui.card().classes('bg-gray-800 flex-1'):
                        ui.label(label).classes('text-gray-400 text-sm')
                        ui.label(value).classes(f'text-xl font-bold {color}')
            
            with ui.card().classes('bg-gray-800 w-full'):
                ui.label('Equity Curve').classes('text-gray-400 text-sm mb-2')
                ui.echart({
                    'backgroundColor': 'transparent',
                    'xAxis': {'type': 'category', 'data': [], 'axisLine': {'lineStyle': {'color': '#666'}}},
                    'yAxis': {'type': 'value', 'axisLine': {'lineStyle': {'color': '#666'}}, 'splitLine': {'lineStyle': {'color': '#333'}}},
                    'series': [{'type': 'line', 'data': [], 'smooth': True, 'areaStyle': {'opacity': 0.3}}],
                    'tooltip': {'trigger': 'axis'},
                    'grid': {'left': '5%', 'right': '5%', 'top': '10%', 'bottom': '10%'}
                }).classes('w-full h-96')
    
    async def _create_settings_page(self) -> None:
        """设置页面"""
        await self._create_header("Settings")
        
        with ui.column().classes('w-full p-4 gap-4'):
            with ui.card().classes('bg-gray-800 w-full'):
                ui.label('Trading Control').classes('text-xl text-white mb-4')
                
                with ui.row().classes('gap-4'):
                    ui.button('Start Trading', on_click=self._start_trading).props('color=green')
                    ui.button('Stop Trading', on_click=self._stop_trading).props('color=orange')
                    ui.button('Emergency Stop', on_click=self._emergency_stop).props('color=red')
            
            with ui.card().classes('bg-gray-800 w-full'):
                ui.label('Circuit Breaker').classes('text-xl text-white mb-4')
                
                with ui.row().classes('gap-4'):
                    ui.button('Trigger Circuit Breaker', on_click=self._trigger_circuit_breaker).props('color=orange')
                    ui.button('Reset Circuit Breaker', on_click=self._reset_circuit_breaker).props('color=blue')
            
            with ui.card().classes('bg-gray-800 w-full'):
                ui.label('System Info').classes('text-xl text-white mb-4')
                
                with ui.column().classes('gap-2'):
                    ui.label('Version: V4.0').classes('text-gray-300')
                    ui.label('Mode: Paper Trading').classes('text-gray-300')
                    ui.label('Connection: Checking...').classes('text-gray-300')
    
    async def _update_dashboard_data(self) -> None:
        """更新仪表盘数据"""
        try:
            if self.system is None:
                if self._status_label:
                    self._status_label.text = 'DEMO MODE'
                    self._status_label.classes('text-2xl font-bold text-blue-400', remove='text-yellow-400 text-green-400 text-red-400')
                return
            
            # 更新状态
            if self._status_label:
                if hasattr(self.system, '_running') and self.system._running:
                    self._status_label.text = 'RUNNING'
                    self._status_label.classes('text-2xl font-bold text-green-400', remove='text-yellow-400 text-blue-400 text-red-400')
                else:
                    self._status_label.text = 'STOPPED'
                    self._status_label.classes('text-2xl font-bold text-red-400', remove='text-yellow-400 text-green-400 text-blue-400')
            
            # 更新引擎状态 (通道、趋势、价格)
            if hasattr(self.system, 'trading_engine') and self.system.trading_engine:
                engine = self.system.trading_engine
                
                # SPX 价格
                if hasattr(self, '_spx_price_label') and self._spx_price_label:
                    spx_price = engine.state.spx_price
                    if spx_price > 0:
                        self._spx_price_label.text = f'${spx_price:,.2f}'
                
                # 通道
                if hasattr(self, '_channel_label') and self._channel_label:
                    channel = engine.state.current_channel
                    if channel.is_valid:
                        self._channel_label.text = f'[{channel.lower:.2f}, {channel.upper:.2f}]'
                    else:
                        self._channel_label.text = '[ - , - ]'
                
                # 趋势
                if hasattr(self, '_trend_label') and self._trend_label:
                    trend = engine.state.current_trend
                    self._trend_label.text = trend.direction
                    if trend.direction == 'BULL':
                        self._trend_label.classes('text-2xl font-bold text-green-400', remove='text-gray-400 text-red-400')
                    elif trend.direction == 'BEAR':
                        self._trend_label.classes('text-2xl font-bold text-red-400', remove='text-gray-400 text-green-400')
                    else:
                        self._trend_label.classes('text-2xl font-bold text-gray-400', remove='text-green-400 text-red-400')
                
                # 信号数
                if hasattr(self, '_signals_count_label') and self._signals_count_label:
                    self._signals_count_label.text = str(engine.state.signals_generated)
            
            # 更新 PnL
            if self._pnl_label:
                daily_pnl = 0.0
                if hasattr(self.system, 'state') and self.system.state:
                    if hasattr(self.system.state, 'get_today_pnl'):
                        daily_pnl = self.system.state.get_today_pnl()
                    elif hasattr(self.system.state, 'get_daily_stats'):
                        stats = self.system.state.get_daily_stats()
                        if stats:
                            daily_pnl = stats.total_pnl
                color = 'text-green-400' if daily_pnl >= 0 else 'text-red-400'
                self._pnl_label.text = f'${daily_pnl:,.2f}'
                self._pnl_label.classes(f'text-2xl font-bold {color}', remove='text-white text-green-400 text-red-400')
            
            # 更新持仓数
            if self._positions_count_label:
                count = 0
                if hasattr(self.system, 'stop_manager') and self.system.stop_manager:
                    if hasattr(self.system.stop_manager, 'get_monitored_positions'):
                        count = len(self.system.stop_manager.get_monitored_positions())
                elif hasattr(self.system, 'state') and self.system.state:
                    if hasattr(self.system.state, 'get_all_positions'):
                        count = len(self.system.state.get_all_positions())
                self._positions_count_label.text = str(count)
            
            # 更新交易数
            if self._trades_count_label:
                trade_count = 0
                if hasattr(self.system, 'state') and self.system.state:
                    if hasattr(self.system.state, 'get_today_trades'):
                        trades = self.system.state.get_today_trades()
                        trade_count = len(trades)
                    elif hasattr(self.system.state, 'get_daily_stats'):
                        stats = self.system.state.get_daily_stats()
                        if stats:
                            trade_count = stats.total_trades
                self._trades_count_label.text = str(trade_count)
                
        except Exception as e:
            logger.error(f"Error updating dashboard: {e}")
    
    def _start_trading(self) -> None:
        if self.system:
            ui.notify('Starting trading...', type='positive')
        else:
            ui.notify('Demo mode - no trading system connected', type='warning')
    
    def _stop_trading(self) -> None:
        if self.system and hasattr(self.system, 'request_shutdown'):
            self.system.request_shutdown()
            ui.notify('Stopping trading...', type='warning')
        else:
            ui.notify('Demo mode - no trading system connected', type='warning')
    
    def _emergency_stop(self) -> None:
        if self.system and hasattr(self.system, 'request_shutdown'):
            self.system.request_shutdown()
            ui.notify('EMERGENCY STOP TRIGGERED!', type='negative')
        else:
            ui.notify('Demo mode - no trading system connected', type='warning')
    
    def _trigger_circuit_breaker(self) -> None:
        if self.system and hasattr(self.system, 'circuit_breaker'):
            asyncio.create_task(self.system.circuit_breaker.trigger("MANUAL"))
            ui.notify('Circuit breaker triggered', type='warning')
        else:
            ui.notify('No circuit breaker available', type='warning')
    
    def _reset_circuit_breaker(self) -> None:
        if self.system and hasattr(self.system, 'circuit_breaker'):
            self.system.circuit_breaker.reset()
            ui.notify('Circuit breaker reset', type='positive')
        else:
            ui.notify('No circuit breaker available', type='warning')
    
    def add_log(self, message: str) -> None:
        """添加日志消息"""
        if self._log_area:
            timestamp = datetime.now().strftime("%H:%M:%S")
            self._log_area.push(f"[{timestamp}] {message}")
    
    def run(self) -> None:
        """独立运行模式"""
        ui.run(
            host=self.host,
            port=self.port,
            title="SPXW Trading Dashboard",
            dark=True,
            reload=False,
            show=False
        )


def run_standalone(host: str = "0.0.0.0", port: int = 8080) -> None:
    """独立运行仪表盘"""
    dashboard = TradingDashboard(host=host, port=port)
    dashboard.run()
