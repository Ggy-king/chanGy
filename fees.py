# -*- coding: utf-8 -*-
"""
手续费/合约规格：启动时拉一次并缓存，距上次更新超 30 天自动重拉（不靠定时任务）。
数据来源 akshare.futures_fees_info()。按合约代码大小写不敏感匹配 config 里的清单。
"""
import os, sys, json, time, datetime as dt, re

sys.path.insert(0, r'E:\agent\chanGy\pylibs')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE = os.path.join(_DIR, config.FEES_CACHE_FILE)
_cache = None


def _key(code):
    """品种字母大写 + 年月数字后三位。兼容 新浪 MA2610 与 接口 MA610 / MA2611 与 JD2611。"""
    m = re.match(r'^([A-Za-z]+)(\d+)$', str(code).strip())
    if not m:
        return str(code).strip().upper()
    return m.group(1).upper() + m.group(2)[-3:]


def _fetch_all():
    import akshare as ak
    df = ak.futures_fees_info()
    out = {}
    for _, r in df.iterrows():
        code = str(r['合约代码']).strip()
        def num(k, d=0.0):
            try:
                v = float(r[k]); return v if v == v else d  # nan guard
            except Exception:
                return d
        out[_key(code)] = {
            'multiplier': num('合约乘数'),
            'min_tick': num('最小跳动'),
            'open_cost': num('开仓费用/手'),
            'close_cost': num('平仓费用/手'),
            'close_today_cost': num('平今费用/手'),
            'margin': num('做多保证金/手'),
        }
    return out


def _stale(path):
    if not os.path.exists(path):
        return True
    age = time.time() - os.path.getmtime(path)
    return age > config.FEES_REFRESH_DAYS * 86400


def load(force=False):
    """返回 {symbol: {multiplier,min_tick,open_cost,close_cost,...}}。"""
    global _cache
    if _cache is not None and not force:
        return _cache
    if not force and not _stale(_CACHE):
        try:
            with open(_CACHE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            _cache = saved['fees']
            return _cache
        except Exception:
            pass
    # 需要更新
    try:
        all_fees = _fetch_all()
    except Exception as e:
        # 拉失败：尽量用旧缓存
        if os.path.exists(_CACHE):
            try:
                with open(_CACHE, 'r', encoding='utf-8') as f:
                    _cache = json.load(f)['fees']
                return _cache
            except Exception:
                pass
        print('[fees] 拉取失败，使用空规格：', e)
        all_fees = {}
    picked = {}
    for c in config.CONTRACTS:
        k = _key(c['symbol'])
        if k in all_fees:
            picked[c['symbol']] = all_fees[k]
    saved = {'updated_at': dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'fees': picked}
    try:
        with open(_CACHE, 'w', encoding='utf-8') as f:
            json.dump(saved, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print('[fees] 写缓存失败：', e)
    _cache = picked
    return _cache


def get(symbol):
    return load().get(symbol, {})


if __name__ == '__main__':
    f = load(force=True)
    print('合约数:', len(f))
    for k, v in list(f.items())[:5]:
        print(k, v)
