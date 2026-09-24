# -*- coding: utf-8 -*-
import os
"""
全局配置：盯盘合约清单 + 级别参数。
- 主力合约切换：只改这里的 symbol（合约代码）即可，其他文件不用动。
- symbol 大小写按新浪期货规则：郑商所大写、大商所小写、上期所大写。
- 级别（区间套）：本级别 15 分钟，次级别 3 分钟，更大级别 60 分钟，日线另行。
"""

# 级别（免费源原生支持 period=3 / 15 / 60；日线用 futures_zh_daily_sina）
PERIOD_SEC = "3"      # 次级别
PERIOD_MAIN = "15"    # 本级别
PERIOD_BIG = "60"     # 更大级别

# 盯盘合约清单（来自文华自选）
CONTRACTS = [
    {"symbol": "jd2611", "name": "鸡蛋",   "exchange": "DCE"},
    {"symbol": "UR2701", "name": "尿素",   "exchange": "CZCE"},
    {"symbol": "MA2610", "name": "甲醇",   "exchange": "CZCE"},
    {"symbol": "jm2701", "name": "焦煤",   "exchange": "DCE"},
    {"symbol": "CF2701", "name": "棉花",   "exchange": "CZCE"},
    {"symbol": "SA2701", "name": "纯碱",   "exchange": "CZCE"},
    {"symbol": "SR2701", "name": "白糖",   "exchange": "CZCE"},
    {"symbol": "v2701",  "name": "PVC",    "exchange": "DCE"},
    {"symbol": "CJ2701", "name": "红枣",   "exchange": "CZCE"},
    {"symbol": "SP2611", "name": "纸浆",   "exchange": "SHFE"},
    {"symbol": "RU2701", "name": "橡胶",   "exchange": "SHFE"},
    {"symbol": "lh2611", "name": "生猪",   "exchange": "DCE"},
    {"symbol": "AP2701", "name": "苹果",   "exchange": "CZCE"},
    {"symbol": "pg2611", "name": "液化气", "exchange": "DCE"},
    {"symbol": "RB2701", "name": "螺纹钢", "exchange": "SHFE"},
    {"symbol": "NI2610", "name": "沪镍",   "exchange": "SHFE"},
    {"symbol": "PK2611", "name": "花生",   "exchange": "CZCE"},
]

# 方便按代码索引
CONTRACT_MAP = {c["symbol"]: c for c in CONTRACTS}

# 手续费缓存文件与有效期（天）
FEES_CACHE_FILE = os.path.join("data", "fees_cache.json")
FEES_REFRESH_DAYS = 30

# ===== 风控（模拟账户）=====
ACCOUNT_CAPITAL = 1000000   # 模拟账户初始资金（元）
RISK_PER_TRADE = 0.01      # 单笔最大风险占账户比例：1%（职业标准，2% 为红线，可调）
ENTRY_RANGE_TICKS = 2      # 建议开仓区间上下各 N 个最小跳动
MARGIN_RATE = 0.10         # 保证金率估算（每手保证金≈最新价×乘数×此值，商品期货普遍8%-15%）
