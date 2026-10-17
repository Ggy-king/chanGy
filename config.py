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
    {"symbol": "jd2612", "name": "鸡蛋",   "exchange": "DCE"},
    {"symbol": "UR2701", "name": "尿素",   "exchange": "CZCE"},
    {"symbol": "MA2701", "name": "甲醇",   "exchange": "CZCE"},
    {"symbol": "jm2701", "name": "焦煤",   "exchange": "DCE"},
    {"symbol": "CF2701", "name": "棉花",   "exchange": "CZCE"},
    {"symbol": "SA2701", "name": "纯碱",   "exchange": "CZCE"},
    {"symbol": "SR2701", "name": "白糖",   "exchange": "CZCE"},
    {"symbol": "v2701",  "name": "PVC",    "exchange": "DCE"},
    {"symbol": "CJ2701", "name": "红枣",   "exchange": "CZCE"},
    {"symbol": "SP2701", "name": "纸浆",   "exchange": "SHFE"},
    {"symbol": "RU2701", "name": "橡胶",   "exchange": "SHFE"},
    {"symbol": "lh2701", "name": "生猪",   "exchange": "DCE"},
    {"symbol": "AP2701", "name": "苹果",   "exchange": "CZCE"},
    {"symbol": "pg2611", "name": "液化气", "exchange": "DCE"},
    {"symbol": "RB2701", "name": "螺纹钢", "exchange": "SHFE"},
    {"symbol": "NI2611", "name": "沪镍",   "exchange": "SHFE"},
    # {"symbol": "PK2611", "name": "花生",   "exchange": "CZCE"},
]

# 方便按代码索引
CONTRACT_MAP = {c["symbol"]: c for c in CONTRACTS}

# ===== 赛道映射（复盘系统"同赛道持仓软提示"用，2026-10-17）=====
# 只做软提示不判违规：当前持仓中同赛道品种重复时，日审计给一句黄灯提示。
# key=品种字母前缀（合约代码去掉数字，换月自动跟随，如 jd2612/jd2701 都命中 'jd'）。
# 未来资金到位新增品种时在这里补赛道（贵金属/油脂油料/新能源等空槽已预留）。
SECTOR_MAP = {
    # —— 当前 16 品种 ——
    'jd': '畜产', 'lh': '畜产',
    'cf': '软商品', 'sr': '软商品',
    'ap': '果品', 'cj': '果品',
    'rb': '黑色建材', 'jm': '煤焦',
    'sa': '地产化工', 'v': '地产化工',
    'ma': '煤化工', 'ur': '煤化工',
    'pg': '原油链',
    'ru': '橡胶', 'sp': '纸浆', 'ni': '有色',
    # —— 未来赛道空槽（资金到位后加品种时同步补这里）——
    # 'au': '贵金属', 'ag': '贵金属',
    # 'm': '油脂油料', 'p': '油脂油料',
    # 'lc': '新能源金属', 'si': '新能源金属',
    # 'sc': '原油（直连，替代液化气）',
}

def sector_of(symbol):
    """合约代码 → 赛道名（按字母前缀匹配）；未知返回 None。"""
    pre = ''.join(ch for ch in str(symbol) if ch.isalpha()).lower()
    return SECTOR_MAP.get(pre)

# ===== 背驰-V反 形态预警系统（docs/背驰反转形态预警系统设计.md）=====
# 监视级别（17品种×5级别=85序列）；扫描节奏按级别分钟数对齐挂钟时间（见 server.pattern_loop）
PATTERN_PERIODS = ['3', '5', '15', '30m', '60']
# 形态C 幅度过滤：6→7 幅度 > 2 × 5→6 幅度 才报三买候选（控制报警频率）
PATTERN_RATIO = 2.0
# （PATTERN_CELL_TTL 已废：事件流回看窗口改为按交易日计，由矩阵页「回看」按钮控制）
# 矩阵页 fired/conf 状态保留时长（秒），到期自动淡化回 idle（也可手动点格子清除）
PATTERN_CELL_TTL = 24 * 3600

# 手续费缓存文件与有效期（天）
FEES_CACHE_FILE = os.path.join("data", "fees_cache.json")
FEES_REFRESH_DAYS = 30

# ===== 风控（模拟账户）=====
ACCOUNT_CAPITAL = 1000000   # 模拟账户初始资金（元）
RISK_PER_TRADE = 0.01      # 单笔最大风险占账户比例：1%（职业标准，2% 为红线，可调）
ENTRY_RANGE_TICKS = 2      # 建议开仓区间上下各 N 个最小跳动
MARGIN_RATE = 0.10         # 保证金率估算（每手保证金≈最新价×乘数×此值，商品期货普遍8%-15%）
