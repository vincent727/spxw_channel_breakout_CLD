# SPXW 0DTE 期权自动交易系统 V4

## 核心特性

### Warm-up 工作流 (配置驱动)

系统实现严格的 "Warm-up -> Subscribe Early -> Immediate Action -> Dynamic Update" 工作流：

```
Phase 1: WARM-UP (预热)
  ├─→ 加载 YAML 配置
  ├─→ 连接 IBKR
  ├─→ 下载历史 K 线 (使用 config.strategy.warmup_bars)
  ├─→ 预计算通道 (使用 config.strategy.channel_lookback)
  └─→ 预计算趋势 (使用 config.strategy.ema_period)

Phase 2: SUBSCRIBE EARLY (提前订阅)
  ├─→ 订阅 5 秒实时 K 线
  └─→ 订阅 Tick 数据
  (即使是 09:20 AM 也立即订阅)

Phase 3: IMMEDIATE ACTION (即时行动)
  ├─→ 开盘后第一个 Tick 立即可用
  └─→ 使用预计算的通道进行突破检查
  (不等待新 K 线形成)

Phase 4: DYNAMIC UPDATE (动态更新)
  ├─→ 信号周期 K 线完成 → 更新通道
  └─→ 趋势周期 K 线完成 → 更新趋势
  (下一个 Tick 立即看到更新)
```

### 零硬编码设计

**严格规则**: Python 代码中不硬编码任何策略参数 (如 `10`, `20`, `0.0005`)

所有参数从 `settings.yaml` 读取:

```yaml
strategy:
  bar_size: "5 mins"           # 信号周期
  trend_bar_size: "1 hour"     # 趋势周期
  warmup_bars: 120             # 预热 K 线数
  channel_lookback: 20         # 通道回看周期
  ema_period: 20               # EMA 周期
  slope_bull_threshold: 0.0005 # 多头斜率阈值
  slope_bear_threshold: -0.0005
  max_concurrent_positions: 3   # 最大并发持仓
```

只需修改 YAML 文件即可改变系统行为，无需修改 Python 代码。

基于 Channel Breakout 策略的 SPX Weekly 0DTE 期权自动交易系统。

## ⚠️ 核心交易约束

### 仅做期权买方 (Long Options Only)
- **仅允许 Buy to Open (BTO)** - 买入 Call 或 Put
- **仅允许 Sell to Close (STC)** - 平仓已持有头寸
- 禁止任何 Sell to Open (STO) 操作
- 系统会自动验证：卖出前必须有足够持仓

### 禁止市价单止损
- 0DTE 期权在极端行情下，做市商会撤单导致巨大滑点
- **止损使用动态追单算法**（三阶段限价单）

## V4 核心特性

| 功能 | 描述 |
|------|------|
| 🎯 动态追单止损 | 三阶段限价止损，避免市价单被收割 |
| ⚡ Tick 级止损 | 不等 K 线收盘，实时监控止损 |
| 🛡️ Bad Tick 过滤 | Mid 偏离 + 跳变检测 + 双重确认 |
| 📦 期权预加载池 | ATM 附近合约预缓存，毫秒级获取 |
| 🔄 事件驱动架构 | 发布/订阅模式，组件解耦 |

## 项目结构

```
spxw_trading/
├── main.py                    # 程序入口
├── config/
│   └── settings.yaml          # 主配置文件
├── core/                      # 核心层
│   ├── event_bus.py           # 事件总线
│   ├── events.py              # 事件定义
│   ├── state.py               # 状态管理
│   └── config.py              # 配置加载
├── execution/                 # 执行层
│   ├── ib_adapter.py          # IBKR API 适配器
│   ├── option_pool.py         # 期权预加载池
│   ├── tick_streamer.py       # Tick 数据流
│   ├── option_selector.py     # 期权选择器
│   └── order_manager.py       # 订单管理器
├── strategy/                  # 策略层
│   ├── channel_breakout.py    # 主策略
│   └── indicators.py          # 技术指标
├── risk/                      # 风控层
│   ├── chase_stop_executor.py # 动态追单止损 ⭐
│   ├── stop_manager.py        # 止损管理器
│   └── circuit_breaker.py     # 熔断器
├── analytics/                 # 分析层
│   └── data_manager.py        # 数据管理
└── tests/                     # 测试
```

## 动态追单止损算法

```
┌─────────────────────────────────────────────────────────────┐
│                    STOP TRIGGERED                            │
│                    Entry: $5.00, Current Bid: $4.00         │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│  PHASE 1: AGGRESSIVE LIMIT                                   │
│  Price = Current Bid - $0.10                                 │
│  Wait: 500ms                                                 │
└─────────────────────────────────────────────────────────────┘
                              │
              ┌───────────────┴───────────────┐
           Filled?                        Not Filled
              │                               │
              ▼                               ▼
           ✅ DONE              ┌─────────────────────────────┐
                                │  PHASE 2: DYNAMIC CHASE      │
                                │  追踪市场下跌，修改限价       │
                                │  Max: 3次追单 / 2秒          │
                                └─────────────────────────────┘
                                              │
                              ┌───────────────┴───────────────┐
                           Filled?                        Chase Failed
                              │                               │
                              ▼                               ▼
                           ✅ DONE              ┌─────────────────────────────┐
                                                │  PHASE 3: PANIC FLUSH       │
                                                │  IF Bid <= $0.20: ABANDON   │
                                                │  ELSE: Deep Limit (Bid*0.8) │
                                                │  Last Resort: Market Order  │
                                                └─────────────────────────────┘
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置

编辑 `config/settings.yaml`:

```yaml
system:
  mode: "paper"  # paper | live

ibkr:
  host: "127.0.0.1"
  port: 7497     # TWS Paper Trading
  client_id: 1
```

### 3. 启动 TWS/Gateway

确保 IBKR TWS 或 Gateway 已启动并启用 API 连接。

### 4. 运行

```bash
# 模拟交易
python main.py --mode paper

# 实盘交易 (谨慎!)
python main.py --mode live
```

## 配置说明

### 风险管理参数

```yaml
risk:
  # 止损阈值
  hard_stop_pct: 0.20           # 20% 硬止损
  breakeven_trigger_pct: 0.15   # 15% 盈利后激活保本
  trailing_activation_pct: 0.30 # 30% 盈利后激活追踪
  trailing_stop_pct: 0.10       # 追踪止损回撤 10%
  
  # 动态追单止损
  chase_stop:
    max_chase_count: 3          # 最大追单次数
    abandon_price_threshold: 0.20  # 低于 $0.20 放弃止损
```

### 策略参数

```yaml
strategy:
  bar_size: "5 mins"
  channel_lookback: 20          # 通道回看期数
  channel_type: "body"          # body | wick
  ema_period: 20
```

## 测试

```bash
pytest tests/ -v
```

## 故障排除 (Troubleshooting)

### 日志文件位置

系统日志文件存储在 `logs/` 目录下，按日期自动分割：

| 文件 | 描述 | 保留天数 |
|------|------|----------|
| `trading_{date}.log` | 完整系统日志 (DEBUG 级别) | 30 天 |
| `trades_{date}.log` | 交易相关日志 | 90 天 |
| `errors_{date}.log` | 错误日志 | 30 天 |

### 如何报告错误

遇到系统错误时，请按以下步骤提交 Issue：

1. **收集日志文件**
   ```bash
   # 查看最近的错误
   tail -100 logs/errors_$(date +%Y-%m-%d).log
   
   # 查看完整日志
   cat logs/trading_$(date +%Y-%m-%d).log
   ```

2. **删除敏感信息**
   
   在分享日志前，请务必删除以下敏感内容：
   - IBKR 账户信息
   - Telegram Bot Token / Chat ID
   - 邮箱账户密码
   - 任何个人财务数据

3. **提交 Issue**
   
   在 GitHub Issue 中包含：
   - 错误描述和复现步骤
   - 相关日志片段（使用代码块格式）
   - 系统环境信息 (Python 版本、操作系统等)
   - `config/settings.yaml` 配置（删除敏感信息后）

**示例 Issue 格式：**

> **问题描述**
> 
> [描述您遇到的问题]
> 
> **复现步骤**
> 1. 启动系统 `python main.py --mode paper`
> 2. 等待开盘信号
> 3. 出现错误...
> 
> **错误日志**
> 
> [粘贴 logs/errors_{date}.log 中的相关内容，使用代码块格式]
> 
> **完整日志片段**
> 
> [粘贴 logs/trading_{date}.log 中的相关内容，使用代码块格式]
> 
> **环境信息**
> - Python 版本: 3.10.x
> - 操作系统: Ubuntu 22.04 / Windows 11 / macOS
> - TWS/Gateway 版本: xxx

### 常见问题

**Q: 连接 IBKR 失败**
```
A: 检查 TWS/Gateway 是否启动，API 设置是否正确：
   - 启用 Socket 客户端
   - 端口设置正确 (Paper: 7497, Live: 7496)
   - 允许的 IP 地址设置正确
```

**Q: 日志文件过大**
```
A: 系统自动保留 30 天日志，可手动清理：
   rm logs/trading_2024-*.log  # 删除旧日志
```

## ⚠️ 风险警告

- 0DTE 期权具有极高风险，可能导致全部本金损失
- 本系统仅供学习和研究使用
- 实盘交易前请充分测试并理解所有风险
- 作者不对任何交易损失负责

## 技术栈

- Python 3.10+
- ib_insync - IBKR API
- asyncio - 异步编程
- pandas/numpy - 数据处理
- aiosqlite - 异步数据库
- pydantic - 配置验证

## License

MIT License
