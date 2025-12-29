#!/usr/bin/env python3
"""
回测运行脚本

SPXW 0DTE 期权自动交易系统 V4

使用方法:
    python run_backtest.py --data data/spx_5min.parquet --start 2024-01-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from datetime import datetime

# 添加项目根目录
sys.path.insert(0, str(Path(__file__).parent))

from core.config import load_config, BacktestConfig
from backtest.engine import BacktestEngine

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def print_progress(progress: float) -> None:
    """打印进度"""
    bar_width = 40
    filled = int(bar_width * progress)
    bar = '█' * filled + '░' * (bar_width - filled)
    print(f'\rProgress: |{bar}| {progress*100:.1f}%', end='', flush=True)


def print_result(result) -> None:
    """打印回测结果"""
    print("\n")
    print("=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    print(f"Period:           {result.start_date} to {result.end_date}")
    print(f"Initial Capital:  ${result.initial_capital:,.2f}")
    print(f"Final Capital:    ${result.final_capital:,.2f}")
    print(f"Total Return:     ${result.total_return:,.2f} ({result.total_return_pct:.1%})")
    print("-" * 60)
    print(f"Total Trades:     {result.total_trades}")
    print(f"Winning Trades:   {result.winning_trades}")
    print(f"Losing Trades:    {result.losing_trades}")
    print(f"Win Rate:         {result.win_rate:.1%}")
    print("-" * 60)
    print(f"Profit Factor:    {result.profit_factor:.2f}")
    print(f"Sharpe Ratio:     {result.sharpe_ratio:.2f}")
    print(f"Max Drawdown:     ${result.max_drawdown:,.2f} ({result.max_drawdown_pct:.1%})")
    print(f"Avg Trade P&L:    ${result.avg_trade_pnl:.2f}")
    print(f"Avg Holding Time: {result.avg_holding_time:.0f}s")
    print("=" * 60)


async def main():
    parser = argparse.ArgumentParser(description="Run SPXW Backtest")
    parser.add_argument(
        "--config",
        type=str,
        default="config/settings.yaml",
        help="Configuration file path"
    )
    parser.add_argument(
        "--data",
        type=str,
        required=True,
        help="Path to historical bar data (Parquet or CSV)"
    )
    parser.add_argument(
        "--start",
        type=str,
        help="Start date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--end",
        type=str,
        help="End date (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--capital",
        type=float,
        default=100000,
        help="Initial capital"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output file for trades (CSV)"
    )
    
    args = parser.parse_args()
    
    # 加载配置
    try:
        trading_config = load_config(args.config)
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return
    
    # 创建回测配置
    backtest_config = BacktestConfig(
        initial_capital=args.capital,
        commission_per_contract=0.65,
        slippage_pct=0.01
    )
    
    # 创建回测引擎
    engine = BacktestEngine(backtest_config, trading_config)
    
    # 加载数据
    try:
        engine.load_data(args.data)
    except Exception as e:
        logger.error(f"Failed to load data: {e}")
        return
    
    # 运行回测
    logger.info("Running backtest...")
    
    result = await engine.run(
        start_date=args.start,
        end_date=args.end,
        progress_callback=print_progress
    )
    
    # 打印结果
    print_result(result)
    
    # 保存交易记录
    if args.output:
        trades_df = engine.get_trades_df()
        trades_df.to_csv(args.output, index=False)
        logger.info(f"Trades saved to {args.output}")
    
    # 按止损类型统计
    if result.trades:
        print("\nStop Type Distribution:")
        stop_types = {}
        for trade in result.trades:
            stop_types[trade.stop_type] = stop_types.get(trade.stop_type, 0) + 1
        
        for st, count in sorted(stop_types.items()):
            print(f"  {st}: {count} ({count/len(result.trades)*100:.1f}%)")


if __name__ == "__main__":
    asyncio.run(main())
