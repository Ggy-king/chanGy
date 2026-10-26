# -*- coding: utf-8 -*-
"""
交易时段判断模块。

用途：节假日 / 周末 / 非交易时段不自动刷新（前端每分钟轮询 + 后端15分钟预警）。
注意：首次进入详情页仍会拉一次数据（由前端控制，本模块只判断"是否允许定时刷新"）。

交易日判断：
  - 优先用 akshare tool_trade_date_hist_sina 拉取交易日历，缓存到 trading_days_cache.json
  - 接口失败时降级：排除周六周日 + 内置主要节假日表

交易时段（统一覆盖所有国内期货品种，各留5分钟余量）：
  上午 08:55 - 11:35
  下午 13:25 - 15:05
  夜盘 20:55 - 次日 02:35
  （凌晨 00:00-02:35 属于前一交易日的夜盘，判断交易日时看前一天）
"""
import datetime as dt
import json
import os
import time

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.path.join('data', 'trading_days_cache.json'))

# 内置节假日兜底（akshare 不可用时使用，日期为当年法定休市日，含调休后的周末补班不在这里——补班日本身是周末但开市，降级模式下会误判，优先用 akshare）
FALLBACK_HOLIDAYS = {
    # 2025
    '2025-01-01',
    '2025-01-28', '2025-01-29', '2025-01-30', '2025-01-31', '2025-02-03', '2025-02-04',
    '2025-04-04',
    '2025-05-01', '2025-05-02', '2025-05-05',
    '2025-05-31', '2025-06-02',
    '2025-10-01', '2025-10-02', '2025-10-03', '2025-10-06', '2025-10-07', '2025-10-08',
    # 2026（预估，以国务院通知为准；akshare 可用时不走到这里）
    '2026-01-01', '2026-01-02',
    '2026-02-16', '2026-02-17', '2026-02-18', '2026-02-19', '2026-02-20',
    '2026-04-06',
    '2026-05-01', '2026-05-04', '2026-05-05',
    '2026-06-19',
    '2026-09-25', '2026-09-26',
    '2026-10-01', '2026-10-02', '2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08',
}

# 交易时段 (start_h, start_m, end_h, end_m)
SESSIONS = [
    (8, 55, 11, 35),
    (13, 25, 15, 5),
    (20, 55, 23, 59),
    (0, 0, 2, 35),
]

_trading_days = None  # set of 'YYYY-MM-DD'，None 表示未加载/降级


def _ensure_pylibs():
    """确保 pylibs 在 sys.path 中（akshare 等依赖装在项目子目录）。"""
    import sys
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'pylibs')
    if p not in sys.path:
        sys.path.insert(0, p)


_trading_days = None
_last_cal_fail = 0.0   # 上次 akshare 拉日历失败时刻（失败后退避10分钟再试，2026-10-26 工程审计：
                       # 原先失败置 None → 之后每次 is_trading_time（每30s）都重试 import+网络，
                       # akshare 故障时周期性冻住调用方线程）


def warmup():
    """启动时预热交易日历（在线程里调，避免网络阻塞事件循环）。"""
    _load_trading_days()


def _load_trading_days():
    """加载交易日历集合，失败返回 None（降级模式）。结果缓存到内存和文件。"""
    global _trading_days, _last_cal_fail
    if _trading_days is not None:
        return _trading_days

    # 1) 读本地缓存
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            today = dt.date.today().isoformat()
            days = data.get('days', [])
            # 缓存里包含今天就直接用（今天已经发生，交易日历一定有记录）
            if today in days:
                _trading_days = set(days)
                return _trading_days
    except Exception:
        pass

    # 2) 拉 akshare（失败后退避：10 分钟内不再碰网络，直接降级周末+内置节假日）
    if time.time() - _last_cal_fail < 600:
        return None
    try:
        _ensure_pylibs()
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        days = set(str(d)[:10] for d in df['trade_date'].tolist())
        _trading_days = days
        _last_cal_fail = 0.0
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({'days': sorted(days)}, f, ensure_ascii=False)
        except Exception:
            pass
        return days
    except Exception as e:
        print(f'[trading_time] akshare 交易日历加载失败，降级为周末+内置节假日（10分钟后重试）: {e}')
        _last_cal_fail = time.time()
        _trading_days = None
        return None


def is_trading_day(d=None):
    """判断某一天是否为交易日。d 为 date 或 datetime，默认今天。"""
    if d is None:
        d = dt.date.today()
    if isinstance(d, dt.datetime):
        d = d.date()
    days = _load_trading_days()
    if days is not None:
        return d.isoformat() in days
    # 降级模式
    if d.weekday() >= 5:  # 周六周日
        return False
    return d.isoformat() not in FALLBACK_HOLIDAYS


def _in_session(t):
    """t 为 datetime.time 或 datetime，判断是否在任一交易时段内。"""
    if isinstance(t, dt.datetime):
        t = t.time()
    mins = t.hour * 60 + t.minute
    for sh, sm, eh, em in SESSIONS:
        s = sh * 60 + sm
        e = eh * 60 + em
        if s <= mins <= e:
            return True
    return False


def is_trading_time(now=None):
    """
    当前是否在交易时段内（含交易日判断）。
    凌晨 00:00-02:35 属于前一交易日的夜盘，交易日看前一天。
    """
    if now is None:
        now = dt.datetime.now()
    # 凌晨夜盘：前一天是否交易日
    if now.hour < 3:
        check_day = now.date() - dt.timedelta(days=1)
    else:
        check_day = now.date()
    if not is_trading_day(check_day):
        return False
    return _in_session(now)


def trading_status(now=None):
    """返回交易状态 dict，供前端展示。"""
    if now is None:
        now = dt.datetime.now()
    if now.hour < 3:
        check_day = now.date() - dt.timedelta(days=1)
    else:
        check_day = now.date()

    if not is_trading_day(check_day):
        return {
            'trading': False,
            'trading_day': False,
            'in_session': False,
            'reason': '非交易日，暂停更新',
            'sessions': [[f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"] for sh, sm, eh, em in SESSIONS],
        }
    in_s = _in_session(now)
    return {
        'trading': in_s,
        'trading_day': True,
        'in_session': in_s,
        'reason': '交易时段' if in_s else '非交易时段，暂停更新',
        'sessions': [[f"{sh:02d}:{sm:02d}", f"{eh:02d}:{em:02d}"] for sh, sm, eh, em in SESSIONS],
    }


if __name__ == '__main__':
    # 自测
    now = dt.datetime.now()
    print('当前时间:', now.strftime('%Y-%m-%d %H:%M:%S %A'))
    st = trading_status()
    print('交易状态:', st)
    # 测几个边界
    tests = [
        dt.datetime(2026, 9, 23, 9, 0),    # 周三交易日上午
        dt.datetime(2026, 9, 23, 8, 50),   # 开盘前
        dt.datetime(2026, 9, 23, 12, 0),   # 午休
        dt.datetime(2026, 9, 23, 21, 0),   # 夜盘
        dt.datetime(2026, 9, 23, 2, 0),    # 凌晨夜盘（看9-22周二）
        dt.datetime(2026, 9, 26, 10, 0),   # 周六
        dt.datetime(2026, 10, 1, 10, 0),   # 国庆
    ]
    for t in tests:
        print(f'  {t.strftime("%Y-%m-%d %H:%M %a")} -> trading={is_trading_time(t)}')


# ==================== 品种级交易时段（trading_sessions.json，2026-10-08） ====================
# 全局 SESSIONS 是所有品种的并集，无法回答"某一个品种此刻有没有行情"。
# 无夜盘品种在夜盘时段里 15s 收不到数据是正常的，按连接死亡判定会引发无谓重建、
# 拖垮整个扫描。品种级时间表由 trading_sessions.json 维护（品种代码，不含合约月份）。

_PRODUCT_SESSIONS = None   # {品种代码: [(start_min, end_min)...]}，end 可 >1440（跨午夜夜盘）


def _load_product_sessions():
    """加载 trading_sessions.json（项目根目录）。加载失败返回空表=未收录（保守全时段）。"""
    global _PRODUCT_SESSIONS
    if _PRODUCT_SESSIONS is not None:
        return _PRODUCT_SESSIONS
    out = {}
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'trading_sessions.json')
        with open(path, encoding='utf-8') as f:
            data = json.load(f)

        def _m(x):
            h, mi = str(x).split(':')
            return int(h) * 60 + int(mi)

        day = [(_m(a), _m(b)) for a, b in data.get('day', [])]
        for prod, cfg in data.get('products', {}).items():
            out[prod.upper()] = day + [(_m(a), _m(b)) for a, b in cfg.get('night', [])]
    except Exception as e:
        print(f'[trading_time] 品种交易时间表加载失败（所有品种按全时段处理）: {e}')
    _PRODUCT_SESSIONS = out
    return out


def product_in_session(product, now=None):
    """品种此刻是否正处于自己的交易时段。未收录品种保守返回 True（视为交易中）。"""
    now = now or dt.datetime.now()
    t = now.hour * 60 + now.minute
    table = _load_product_sessions()
    ivs = table.get(str(product).upper())
    if ivs is None:
        return True
    for s, e in ivs:
        if e > 1440:            # 跨午夜夜盘（如 21:00-25:00）
            if t >= s or t < e - 1440:
                return True
        elif s <= t < e:
            return True
    return False


def any_product_in_session(now=None):
    """任一已收录品种仍在交易（用于连接重建判定的精准窗口）。表为空=保守 True。"""
    table = _load_product_sessions()
    if not table:
        return True
    return any(product_in_session(p, now) for p in table)
