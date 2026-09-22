# -*- coding: utf-8 -*-
"""
多品种行情引擎
- 循环拉取 config.CONTRACTS 各品种 15 分钟 K -> K线合并/顶底分型/笔 -> 快照
- 每个品种：完整详情（K线/笔/买卖点 markers）+ 统计（配对交易胜率/盈亏比/回撤/盈亏）
- 风控：模拟账户 10 万，单笔最大风险 1%，按止损距离反推手数，给出开仓区间/保证金
- 带锁缓存；新信号按品种独立去重；手续费来自 fees.py
"""
import sys, os, re, json, threading, time, datetime as dt
import asyncio
import requests
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'pylibs'))
sys.path.insert(0, _HERE)

import akshare as ak
from chanlun import merge_klines, find_fenxing, build_bi
import config, fees

_lock = threading.Lock()
_cache_overview = None
_cache_detail = {}     # symbol -> 单品种完整快照
_pushed = {}          # symbol -> (side, time) 已推送基线

# 看盘模式3分钟K线缓存：symbol -> {时间字符串: K线dict}
# 已完成的K线不可变，缓存让可回看的历史超过新浪1分钟接口的窗口（越看越多）；
# 仅内存存储，服务重启自动清空
_live3_cache = {}
_LIVE3_CACHE_MAX = 3000  # 每品种缓存上限（≈3~4周的3分钟K线，内存可忽略）

# 回测统计冻结：显式回测结果落盘，启动时加载；
# 15分钟常规刷新不再重算，由 update_frozen_stats（回测按钮触发）覆盖
_DIR = os.path.dirname(os.path.abspath(__file__))
_STATS_CACHE_FILE = os.path.join(_DIR, '.stats_cache.json')


def _load_stats_cache():
    try:
        if os.path.exists(_STATS_CACHE_FILE):
            with open(_STATS_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print('load stats cache error:', e)
    return {}


def _save_stats_cache():
    try:
        with open(_STATS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_stats_frozen, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('save stats cache error:', e)


_stats_frozen = _load_stats_cache()

# 并发抓取线程池（akshare 对新浪接口有频率限制，6 并发安全且最快）
_FETCH_WORKERS = 6
_pool = ThreadPoolExecutor(max_workers=_FETCH_WORKERS, thread_name_prefix='cl-fetcher')


def _dd_stats(curve):
    """累计盈亏资金曲线 -> (最大回撤, 平均回撤)。单位=元。"""
    if len(curve) < 2:
        return 0.0, 0.0
    peak = curve[0]
    events = []
    cur_dd = 0.0
    for v in curve[1:]:
        if v >= peak:
            if cur_dd > 0:
                events.append(cur_dd)
            peak = v
            cur_dd = 0.0
        else:
            cur_dd = peak - v
    if cur_dd > 0:
        events.append(cur_dd)
    mx = max(events) if events else 0.0
    avg = sum(events) / len(events) if events else 0.0
    return round(mx, 1), round(avg, 1)


def _risk_info(side, price, stop_loss, fee, last_price):
    """根据账户/风险比例/止损距离，算建议手数/开仓区间/保证金。"""
    mult = fee.get('multiplier', 10) or 10
    tick = fee.get('min_tick', 1) or 1
    if side != 'buy' or stop_loss is None:
        return None
    stop_pts = abs(price - stop_loss)
    if stop_pts <= 0:
        stop_pts = tick  # 至少一个最小跳动
    risk_amt = config.ACCOUNT_CAPITAL * config.RISK_PER_TRADE
    per_lot_risk = stop_pts * mult
    lots = int(risk_amt // per_lot_risk) if per_lot_risk > 0 else 0
    rng = tick * config.ENTRY_RANGE_TICKS
    margin = round(last_price * mult * config.MARGIN_RATE, 0)
    return {
        'stop_loss': round(stop_loss, 1),
        'stop_pts': round(stop_pts, 1),
        'risk_amount': round(risk_amt, 0),
        'lots': lots,
        'entry_low': round(price - rng, 1),
        'entry_high': round(price + rng, 1),
        'margin_per_lot': margin,
        'open_cost': fee.get('open_cost', 0),
        'close_cost': fee.get('close_cost', 0),
        'multiplier': mult,
    }


def _build(df, symbol, name):
    df = df.reset_index(drop=True)
    df['datetime'] = __import__('pandas').to_datetime(df['datetime'])

    merged = merge_klines(df)
    top_fx, bottom_fx = find_fenxing(merged)
    bi_list = build_bi(top_fx, bottom_fx, merged)

    kline, vol = [], []
    for _, r in df.iterrows():
        ts = int(r['datetime'].timestamp())
        kline.append({'time': ts, 'open': round(r['open'], 1), 'high': round(r['high'], 1),
                      'low': round(r['low'], 1), 'close': round(r['close'], 1)})
        vol.append({'time': ts, 'value': int(r['volume'])})

    bi_line = []
    if bi_list:
        s0 = merged['low_idx'].iloc[bi_list[0]['start_idx']]
        bi_line.append({'time': int(df['datetime'].iloc[s0].timestamp()),
                        'value': bi_list[0]['start_price']})
        for b in bi_list:
            col = 'high_idx' if b['direction'] == 'up' else 'low_idx'
            e = merged[col].iloc[b['end_idx']]
            bi_line.append({'time': int(df['datetime'].iloc[e].timestamp()),
                            'value': b['end_price']})

    markers, signals = [], []
    for b in bi_list:
        ts = int(df['datetime'].iloc[0].timestamp())
        if b['direction'] == 'up':
            raw = int(merged['low_idx'].iloc[b['start_idx']])
            ts = int(df['datetime'].iloc[raw].timestamp())
            buy_price = round(float(df['close'].iloc[raw]), 1)
            stop = round(float(df['low'].iloc[raw]), 1)
            markers.append({'time': ts, 'position': 'belowBar', 'color': '#ef5350',
                            'shape': 'arrowUp', 'text': '买'})
            signals.append({'side': 'buy', 'time': ts, 'price': buy_price,
                            'stop_loss': stop, 'raw_idx': raw,
                            'label': '底分型确认·买入',
                            'reason': '15分底分型确认，向上笔起点'})
        else:
            raw = int(merged['high_idx'].iloc[b['start_idx']])
            ts = int(df['datetime'].iloc[raw].timestamp())
            sell_price = round(float(df['close'].iloc[raw]), 1)
            markers.append({'time': ts, 'position': 'aboveBar', 'color': '#26a69a',
                            'shape': 'arrowDown', 'text': '卖'})
            signals.append({'side': 'sell', 'time': ts, 'price': sell_price,
                            'label': '顶分型确认·平多',
                            'reason': '15分顶分型确认，向下笔起点，平多止盈'})

    last_price = round(float(df['close'].iloc[-1]), 1)
    prev_price = round(float(df['close'].iloc[-2]), 1)
    change = round(last_price - prev_price, 1)
    change_pct = round(change / prev_price * 100, 2)

    return {
        'symbol': symbol, 'name': name, 'period': config.PERIOD_MAIN,
        'updated_at': str(df['datetime'].iloc[-1]),
        'bar_count': len(df), 'bi_count': len(bi_list),
        'last_price': last_price, 'change': change, 'change_pct': change_pct,
        'kline': kline, 'vol': vol, 'bi': bi_line,
        'markers': markers, 'signals': signals,
        'last_signal': signals[-1] if signals else None,
        'error': None,
    }


def _fetch_one(symbol, retries=3, period=None):
    """拉取单品种分钟 K 线，带指数退避重试。新浪接口偶发 429/超时常见。"""
    period = period or config.PERIOD_MAIN
    last = None
    for i in range(retries):
        try:
            df = ak.futures_zh_minute_sina(symbol=symbol, period=period)
            if df is None or len(df) == 0:
                raise RuntimeError('empty data')
            return df
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(0.8 * (2 ** i))
    raise RuntimeError(f'fetch {symbol} failed after {retries} retries: {last}')


def build_symbol(symbol):
    name = config.CONTRACT_MAP[symbol]['name']
    df = _fetch_one(symbol)
    snap = _build(df, symbol, name)
    fee = fees.get(symbol)
    snap['fee'] = fee
    # 不自动跑回测统计：只有用户显式点“回测”后，结果才会写进 _stats_frozen 并落盘。
    # 启动/15分钟常规刷新只关心 K 线、分型、笔、最新信号；没有回测统计的品种 stats 为空。
    with _lock:
        snap['stats'] = _stats_frozen.get(symbol)
    snap['trade_list'] = []
    # 给最近一个买入信号挂上风控建议（只做多，平仓信号不影响买入建议）
    last_buy = None
    for x in reversed(snap.get('signals', [])):
        if x.get('side') == 'buy':
            last_buy = x
            break
    if last_buy:
        snap['risk'] = _risk_info('buy', last_buy['price'], last_buy.get('stop_loss'),
                                  fee, snap['last_price'])
        snap['last_buy'] = last_buy
    else:
        snap['risk'] = None
        snap['last_buy'] = None
    # 每手保证金（随最新价）
    snap['margin_per_lot'] = round(snap['last_price'] * (fee.get('multiplier', 10) or 10)
                                   * config.MARGIN_RATE, 0)
    return snap


def _overview_item(snap):
    ls = snap.get('last_signal')
    if ls:
        ls = dict(ls)
        if ls['side'] == 'buy' and snap.get('risk'):
            ls['risk'] = snap['risk']
    return {
        'symbol': snap['symbol'], 'name': snap['name'],
        'last_price': snap['last_price'], 'change': snap['change'],
        'change_pct': snap['change_pct'], 'bi_count': snap['bi_count'],
        'bar_count': snap['bar_count'], 'updated_at': snap['updated_at'],
        'margin_per_lot': snap.get('margin_per_lot'),
        'last_signal': ls, 'stats': snap.get('stats'), 'error': snap.get('error'),
    }


def _build_overview_from_config_and_stats():
    """启动时无缓存，用 config.CONTRACTS + 持久化回测统计拼一个可展示的总览骨架。

    - 品种名、回测统计（胜率/回撤/交易笔数等）立即显示
    - 价格、涨跌、信号、笔数先空，由 /api/quotes 和 15 分钟后台刷新逐步填充
    - 不拉取任何 K 线，不阻塞列表页
    """
    items = []
    for c in config.CONTRACTS:
        sym = c['symbol']
        st = _stats_frozen.get(sym)
        items.append({
            'symbol': sym, 'name': c['name'],
            'last_price': None, 'change': None, 'change_pct': None,
            'bi_count': None, 'bar_count': None,
            'updated_at': None, 'margin_per_lot': None,
            'last_signal': None, 'stats': st, 'error': None,
        })
    ok = [x for x in items if not x.get('error')]
    total_sig = sum((x['stats'] or {}).get('trades', 0) for x in ok)
    total_money = round(sum((x['stats'] or {}).get('total_money', 0) for x in ok), 1)
    win_rs = [x['stats']['win_rate'] for x in ok
              if x.get('stats') and x['stats'].get('win_rate') is not None]
    pl_rs = [x['stats']['pl_ratio'] for x in ok
             if x.get('stats') and x['stats'].get('pl_ratio') is not None]
    dd_list = [x['stats']['max_dd'] for x in ok
               if x.get('stats') and x['stats'].get('max_dd')]
    global_stats = {
        'watch': len(ok), 'failed': 0,
        'total_trades': total_sig,
        'total_money': total_money,
        'avg_win_rate': round(sum(win_rs) / len(win_rs), 1) if win_rs else None,
        'avg_pl_ratio': round(sum(pl_rs) / len(pl_rs), 2) if pl_rs else None,
        'max_dd': max(dd_list) if dd_list else 0,
        'avg_dd': 0,
        'today_signals': 0,
    }
    return {'updated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'account_capital': config.ACCOUNT_CAPITAL,
            'risk_pct': config.RISK_PER_TRADE,
            'items': items, 'global': global_stats}


def get_overview(force=False):
    global _cache_overview
    with _lock:
        if _cache_overview and not force:
            return _cache_overview
    if not force:
        # 无缓存（刚启动，后台并行首拉进行中）：用配置+持久化回测统计立即拼一个总览骨架。
        # 价格/信号先空，页面秒开、行可点；等 /api/quotes 和 15分钟刷新逐步填上新鲜数据。
        ov = _build_overview_from_config_and_stats()
        with _lock:
            _cache_overview = ov
        return ov
    items = []
    all_trades = []
    for c in config.CONTRACTS:
        sym = c['symbol']
        try:
            snap = build_symbol(sym)
            with _lock:
                _cache_detail[sym] = snap
            items.append(_overview_item(snap))
            all_trades.extend(snap.get('trade_list', []))
        except Exception as e:
            items.append({'symbol': sym, 'name': c['name'], 'error': str(e)[:80]})
    ok = [x for x in items if not x.get('error')]
    total_sig = sum((x['stats'] or {}).get('trades', 0) for x in ok)
    total_money = round(sum((x['stats'] or {}).get('total_money', 0) for x in ok), 1)
    win_rs = [x['stats']['win_rate'] for x in ok if x.get('stats') and x['stats'].get('win_rate') is not None]
    pl_rs = [x['stats']['pl_ratio'] for x in ok if x.get('stats') and x['stats'].get('pl_ratio') is not None]
    dd_list = [x['stats']['max_dd'] for x in ok if x.get('stats') and x['stats'].get('max_dd')]
    # 跨品种合并资金曲线（按交易时间排序）
    all_trades.sort(key=lambda t: t['time'])
    curve, cum = [], 0.0
    for t in all_trades:
        cum += t['money']
        curve.append(cum)
    mdd, add = _dd_stats(curve)
    global_stats = {
        'watch': len(ok), 'failed': len(items) - len(ok),
        'total_trades': total_sig,
        'total_money': total_money,
        'avg_win_rate': round(sum(win_rs) / len(win_rs), 1) if win_rs else None,
        'avg_pl_ratio': round(sum(pl_rs) / len(pl_rs), 2) if pl_rs else None,
        'max_dd': mdd,
        'avg_dd': add,
        'today_signals': len([x for x in ok if x.get('last_signal')]),
    }
    ov = {'updated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
          'account_capital': config.ACCOUNT_CAPITAL,
          'risk_pct': config.RISK_PER_TRADE,
          'items': items, 'global': global_stats}
    with _lock:
        _cache_overview = ov
    return ov


def get_detail(symbol, force=False):
    with _lock:
        if not force and symbol in _cache_detail:
            return _cache_detail[symbol]
    snap = build_symbol(symbol)
    with _lock:
        _cache_detail[symbol] = snap
    return snap


def simulate(symbol, side):
    """PC端手动模拟一个信号：按当前数据算好止损/手数，返回完整信号 dict。"""
    snap = get_detail(symbol)
    last = snap.get('last_signal') or {}
    price = last.get('price', snap['last_price'])
    fee = fees.get(symbol)
    risk = None
    stop = None
    if side == 'buy':
        stop = last.get('stop_loss')
        if stop is None:
            stop = round(price - (fee.get('min_tick', 1) or 1) * 3, 1)
        risk = _risk_info('buy', price, stop, fee, snap['last_price'])
    return {
        'symbol': symbol, 'name': snap['name'], 'side': side,
        'price': round(price, 1), 'time': int(time.time()),
        'reason': '【手动模拟】' + ('底分型买入信号' if side == 'buy' else '顶分型卖出信号'),
        'risk': risk, 'simulated': True,
    }


def refresh_and_detect_all():
    """刷新全部品种；返回 (总览, 新信号列表)。新信号才推。"""
    ov = get_overview(force=True)
    new_sigs = []
    for c in config.CONTRACTS:
        sym = c['symbol']
        snap = _cache_detail.get(sym)
        if not snap:
            continue
        sig = snap.get('last_signal')
        key = (sig['side'], sig['time']) if sig else None
        old = _pushed.get(sym)
        if key and key != old:
            _pushed[sym] = key
            r = {'symbol': sym, 'name': snap['name'], 'side': sig['side'],
                 'price': sig['price'], 'time': sig['time'],
                 'reason': sig.get('reason') or sig.get('label', '')}
            if sig['side'] == 'buy':
                r['risk'] = snap.get('risk')
            new_sigs.append(r)
        elif key:
            _pushed[sym] = key
    return ov, new_sigs


async def refresh_and_detect_all_async():
    """并行刷新全部品种（线程池+信号量限并发）；返回 (总览, 新信号列表)。"""
    loop = asyncio.get_event_loop()
    sem = asyncio.Semaphore(_FETCH_WORKERS)

    async def one(sym):
        async with sem:
            return await loop.run_in_executor(_pool, build_symbol, sym)

    snaps = await asyncio.gather(
        *[one(c['symbol']) for c in config.CONTRACTS],
        return_exceptions=True,
    )
    items, all_trades = [], []
    for c, snap in zip(config.CONTRACTS, snaps):
        sym = c['symbol']
        if isinstance(snap, BaseException):
            items.append({'symbol': sym, 'name': c['name'], 'error': str(snap)[:80]})
            continue
        with _lock:
            _cache_detail[sym] = snap
        items.append(_overview_item(snap))
        all_trades.extend(snap.get('trade_list', []))
    ok = [x for x in items if not x.get('error')]
    total_sig = sum((x['stats'] or {}).get('trades', 0) for x in ok)
    total_money = round(sum((x['stats'] or {}).get('total_money', 0) for x in ok), 1)
    win_rs = [x['stats']['win_rate'] for x in ok if x.get('stats') and x['stats'].get('win_rate') is not None]
    pl_rs = [x['stats']['pl_ratio'] for x in ok if x.get('stats') and x['stats'].get('pl_ratio') is not None]
    dd_list = [x['stats']['max_dd'] for x in ok if x.get('stats') and x['stats'].get('max_dd')]
    all_trades.sort(key=lambda t: t['time'])
    curve, cum = [], 0.0
    for t in all_trades:
        cum += t['money']
        curve.append(cum)
    mdd, add = _dd_stats(curve)
    global_stats = {
        'watch': len(ok), 'failed': len(items) - len(ok),
        'total_trades': total_sig,
        'total_money': total_money,
        'avg_win_rate': round(sum(win_rs) / len(win_rs), 1) if win_rs else None,
        'avg_pl_ratio': round(sum(pl_rs) / len(pl_rs), 2) if pl_rs else None,
        'max_dd': mdd,
        'avg_dd': add,
        'today_signals': len([x for x in ok if x.get('last_signal')]),
    }
    ov = {'updated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
          'account_capital': config.ACCOUNT_CAPITAL,
          'risk_pct': config.RISK_PER_TRADE,
          'items': items, 'global': global_stats}
    with _lock:
        _cache_overview = ov
    # 检测新信号（与 sync 版同口径）
    new_sigs = []
    for c in config.CONTRACTS:
        sym = c['symbol']
        snap = _cache_detail.get(sym)
        if not snap:
            continue
        sig = snap.get('last_signal')
        key = (sig['side'], sig['time']) if sig else None
        old = _pushed.get(sym)
        if key and key != old:
            _pushed[sym] = key
            r = {'symbol': sym, 'name': snap['name'], 'side': sig['side'],
                 'price': sig['price'], 'time': sig['time'],
                 'reason': sig.get('reason') or sig.get('label', '')}
            if sig['side'] == 'buy':
                r['risk'] = snap.get('risk')
            new_sigs.append(r)
        elif key:
            _pushed[sym] = key
    return ov, new_sigs


def update_frozen_stats(symbol, stats):
    """显式回测完成后，用回测结果覆盖列表页该品种的统计（并冻结）。
    常规刷新不再改统计，直到下一次显式回测。"""
    if not stats:
        return
    with _lock:
        _stats_frozen[symbol] = dict(stats)
        _save_stats_cache()   # 显式回测结果立即落盘，重启后仍可见
        snap = _cache_detail.get(symbol)
        if snap is not None:
            snap['stats'] = dict(stats)
        if _cache_overview:
            for it in _cache_overview.get('items', []):
                if it.get('symbol') == symbol and not it.get('error'):
                    it['stats'] = dict(stats)
            _recompute_overview_global(_cache_overview)


def _recompute_overview_global(ov):
    """按 items 里各品种的 stats 重算顶部汇总卡片。"""
    if not ov or not ov.get('items'):
        return
    ok = [x for x in ov['items'] if not x.get('error')]
    win_rs = [x['stats']['win_rate'] for x in ok
              if x.get('stats') and x['stats'].get('win_rate') is not None]
    pl_rs = [x['stats']['pl_ratio'] for x in ok
             if x.get('stats') and x['stats'].get('pl_ratio') is not None]
    dd_list = [x['stats']['max_dd'] for x in ok
               if x.get('stats') and x['stats'].get('max_dd')]
    g = ov.setdefault('global', {})
    g['watch'] = len(ok)
    g['failed'] = len(ov['items']) - len(ok)
    g['total_trades'] = sum((x['stats'] or {}).get('trades', 0) for x in ok)
    g['total_money'] = round(sum((x['stats'] or {}).get('total_money', 0) for x in ok), 1)
    g['avg_win_rate'] = round(sum(win_rs) / len(win_rs), 1) if win_rs else None
    g['avg_pl_ratio'] = round(sum(pl_rs) / len(pl_rs), 2) if pl_rs else None
    g['max_dd'] = max(dd_list) if dd_list else 0


def get_quotes():
    """轻量实时行情：一次请求拉全部品种最新价（新浪hq接口）。
    只更新 最新价/涨跌/保证金，不重拉K线、不重算笔和统计。"""
    syms = [c['symbol'] for c in config.CONTRACTS]
    url = 'https://hq.sinajs.cn/list=' + ','.join('nf_' + s for s in syms)
    r = requests.get(url, headers={'Referer': 'https://finance.sina.com.cn'}, timeout=6)
    r.encoding = 'gbk'
    out = {}
    for line in r.text.splitlines():
        m = re.match(r'var hq_str_nf_(\w+)="(.*)"', line.strip())
        if not m:
            continue
        sym, f = m.group(1), m.group(2).split(',')
        if len(f) < 11 or not f[8]:
            continue  # 非交易时段个别品种可能返回空
        try:
            price, prev_settle = float(f[8]), float(f[10])
        except ValueError:
            continue
        if price <= 0 or prev_settle <= 0:
            continue
        fee = fees.get(sym)
        mult = fee.get('multiplier', 10) or 10
        change = round(price - prev_settle, 1)
        out[sym] = {
            'price': round(price, 1),
            'change': change,
            'change_pct': round(change / prev_settle * 100, 2),
            'margin_per_lot': round(price * mult * config.MARGIN_RATE, 0),
        }
    return {'updated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'quotes': out}


# ==================== 看盘模式（多周期，独立于15分钟预警） ====================

def _merge_to_period(df1m, n):
    """把1分钟K线合成 n 分钟K线（n=3 等非原生周期）。

    按真实时间对齐（修复"按位置分组导致历史K线每分钟重排"的bug）：
    - 新浪1分钟K线按"结束时刻"打标（09:01 这根代表 09:00-09:01），
      故按 ceil(n分钟) 分组：09:01/09:02/09:03 合成标签 09:03 的一根；
    - 各交易时段起止都是 n 的整数倍（09:00/11:30/13:30/15:00/21:00/23:00…），
      对齐后天然不会把休息时段两侧的K线缝进同一根；
    - 开头不完整的一组（数据窗口起点落在某个 n 分钟中间，缺前面的1分钟K线）丢弃；
    - 分组只取决于钟表时间，与拉取窗口的起点无关，历史K线不再每分钟重排。
    """
    import pandas as pd
    df = df1m.reset_index(drop=True).copy()
    df['datetime'] = pd.to_datetime(df['datetime'])
    df['_grp'] = df['datetime'].dt.ceil(f'{n}min')
    agg = df.groupby('_grp').agg({
        'open': 'first', 'high': 'max', 'low': 'min',
        'close': 'last', 'volume': 'sum', 'datetime': 'count',
    }).rename(columns={'datetime': '_cnt'})
    agg['datetime'] = agg.index  # 标签取桶的结束时刻（与新浪原生15分钟口径一致）
    agg = agg.reset_index(drop=True)
    # 丢弃开头不完整的一组；末尾正在形成的一根保留（本就应实时变化）
    if len(agg) > 1 and int(agg['_cnt'].iloc[0]) < n:
        agg = agg.iloc[1:]
    return agg.drop(columns=['_cnt']).reset_index(drop=True)


def _merge_and_cache_3m(symbol, df1m):
    """合成3分钟K线并与内存缓存合并后返回完整序列。

    对齐修复后已完成的K线是 immutable 的（同一历史时段的合成结果永远一致），
    所以合并规则很简单：按时间戳键覆盖；缓存里超出1分钟接口窗口的旧K线一直保留，
    看盘越久可回看的3分钟历史越长。仅内存存储，服务重启自动清空。"""
    import pandas as pd
    df_new = _merge_to_period(df1m, 3)
    with _lock:
        cache = _live3_cache.setdefault(symbol, {})
        for r in df_new.itertuples():
            bar = {'datetime': r.datetime, 'open': float(r.open), 'high': float(r.high),
                   'low': float(r.low), 'close': float(r.close), 'volume': float(r.volume)}
            cache[str(r.datetime)] = bar
        if len(cache) > _LIVE3_CACHE_MAX:  # 超上限裁掉最旧的
            for k in sorted(cache.keys())[:len(cache) - _LIVE3_CACHE_MAX]:
                del cache[k]
        return pd.DataFrame([cache[k] for k in sorted(cache.keys())])


def get_live(symbol, period='15'):
    """看盘模式：拉指定周期K线，算合并/分型/笔，返回精简快照。
    period: '1','3','15','60','daily'。3分钟由1分钟合成，日线用日线接口。
    完全独立，不写入 _cache_detail，不影响15分钟预警与回测。"""
    name = config.CONTRACT_MAP[symbol]['name']
    if period == 'daily':
        df = ak.futures_zh_daily_sina(symbol=symbol)
        if df is not None and 'date' in df.columns:
            df = df.rename(columns={'date': 'datetime'})
    elif period == '3':
        df1m = _fetch_one(symbol, period='1')
        df = _merge_and_cache_3m(symbol, df1m)
    else:
        df = ak.futures_zh_minute_sina(symbol=symbol, period=period)
    if df is None or len(df) == 0:
        raise RuntimeError(f'empty {period} data')
    snap = _build(df, symbol, name)
    snap['period'] = period
    label = period + ('分' if period != 'daily' else '线')
    for s in snap.get('signals', []):
        s['reason'] = s.get('reason', '').replace('15分', label)
        s['label'] = s.get('label', '').replace('15分', label)
    return {
        'symbol': snap['symbol'], 'name': snap['name'], 'period': period,
        'updated_at': snap['updated_at'],
        'bar_count': snap['bar_count'], 'bi_count': snap['bi_count'],
        'last_price': snap['last_price'],
        'change': snap['change'], 'change_pct': snap['change_pct'],
        'kline': snap['kline'], 'vol': snap.get('vol', []),
        'bi': snap['bi'], 'markers': snap['markers'],
    }
