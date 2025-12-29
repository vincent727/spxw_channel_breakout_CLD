#!/usr/bin/env python3
"""
SPXW 0DTE 交易系统 - Web UI 启动脚本

独立运行仪表盘，用于监控交易系统状态

使用方法:
    python run_ui.py                # 使用默认端口 8080
    python run_ui.py --port 8888    # 使用指定端口
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from ui.dashboard import TradingDashboard


def main():
    parser = argparse.ArgumentParser(description="Run SPXW Trading Dashboard")
    parser.add_argument(
        "--host",
        type=str,
        default="0.0.0.0",
        help="Host to bind (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to bind (default: 8080)"
    )
    
    args = parser.parse_args()
    
    print(f"Starting SPXW Trading Dashboard on http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop")
    
    dashboard = TradingDashboard(
        trading_system=None,  # 独立模式，无实时数据
        host=args.host,
        port=args.port
    )
    
    dashboard.run()


if __name__ == "__main__":
    main()
