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
import tqsdk_data as tq

# 天勤周期映射（秒数）
_TQ_PERIOD = {'30':30, '1':60, '3':180, '15':900, '60':3600, 'daily':86400}

_lock = threading.Lock()
_cache_overview = None
_cache_detail = {}     # symbol -> 单品种完整快照
_pushed = {}          # symbol -> (side, time) 已推送基线

# 回测统计冻结：显式回测结果落盘，启动时加载；
# 15分钟常规刷新不再重算，由 update_frozen_stats（回测按钮触发）覆盖
_DIR = os.path.dirname(os.path.abspath(__file__))
_STATS_CACHE_FILE = os.path.join(_DIR, os.path.join('data', '.stats_cache.json'))


def _load_stats_cache():
    """加载回测统计缓存；自动剔除已不在 config.CONTRACTS 盯盘清单里的旧合约（换月自动替换）。"""
    data = {}
    try:
        if os.path.exists(_STATS_CACHE_FILE):
            with open(_STATS_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
    except Exception as e:
        print('load stats cache error:', e)
    valid = set(config.CONTRACT_MAP)
    return {k: v for k, v in data.items() if k in valid}


def _save_stats_cache():
    try:
        os.makedirs(os.path.dirname(_STATS_CACHE_FILE), exist_ok=True)
        valid = set(config.CONTRACT_MAP)   # 落盘前同样过滤，不把已换下的旧合约写回去
        data = {k: v for k, v in _stats_frozen.items() if k in valid}
        with open(_STATS_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('save stats cache error:', e)


_stats_frozen = _load_stats_cache()
# 磁盘上若残留已换下的旧合约（如换月 MA2610→MA2611），启动时立即重写清理
try:
    if os.path.exists(_STATS_CACHE_FILE):
        with open(_STATS_CACHE_FILE, 'r', encoding='utf-8') as _f:
            if set(json.load(_f)) != set(_stats_frozen):
                _save_stats_cache()
except Exception:
    pass

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


def _build(df, symbol, name, allow_rollback=True):
    df = df.reset_index(drop=True)
    # datetime已经是带时区的北京时间，只在无时区时才转换，避免.timestamp()差8小时
    if df['datetime'].dt.tz is None:
        df['datetime'] = __import__('pandas').to_datetime(df['datetime'])

    merged = merge_klines(df)
    top_fx, bottom_fx = find_fenxing(merged)
    bi_list = build_bi(top_fx, bottom_fx, merged, allow_rollback=allow_rollback)

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
    """拉取单品种K线（天勤数据源），带重试。"""
    period = period or config.PERIOD_MAIN
    period_sec = _TQ_PERIOD.get(period, 900)
    last = None
    for i in range(retries):
        try:
            df = tq.fetch_kline(symbol, period_sec, data_length=2000)
            if df is None or len(df) == 0:
                raise RuntimeError('empty data')
            return df
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(0.8 * (2 ** i))
    raise RuntimeError(f'fetch {symbol} failed after {retries} retries: {last}')


def build_symbol(symbol, allow_rollback=True):
    name = config.CONTRACT_MAP[symbol]['name']
    df = _fetch_one(symbol)
    snap = _build(df, symbol, name, allow_rollback=allow_rollback)
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


def get_live(symbol, period='15', allow_rollback=True):
    """看盘模式：拉指定周期K线（天勤数据源），算合并/分型/笔，返回精简快照。
    period: '30','1','3','15','60','daily'。全部用天勤原生接口，无需合成。
    完全独立，不写入 _cache_detail，不影响15分钟预警与回测。"""
    name = config.CONTRACT_MAP[symbol]['name']
    period_sec = _TQ_PERIOD.get(period, 900)
    df = tq.fetch_kline(symbol, period_sec, data_length=2000)
    if df is None or len(df) == 0:
        raise RuntimeError(f'empty {period} data')
    snap = _build(df, symbol, name, allow_rollback=allow_rollback)
    snap['period'] = period
    label = period + ('秒' if period == '30' else ('分' if period != 'daily' else '线'))
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
