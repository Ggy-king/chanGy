# -*- coding: utf-8 -*-
"""
价格预警管理模块
- 右键K线图添加预警（上穿/下穿）
- 定时检查价格，触发后通过WebSocket推送
- 预警持久化到JSON文件
"""
import json, os, time, threading
from datetime import datetime

_ALERT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.path.join('data', 'alerts.json'))
_alerts = []
_last_prices = {}  # {symbol: last_price}
_lock = threading.Lock()
_callbacks = []  # 触发时的回调函数列表


def _load():
    global _alerts
    if os.path.exists(_ALERT_FILE):
        try:
            with open(_ALERT_FILE, 'r', encoding='utf-8') as f:
                _alerts = json.load(f)
        except:
            _alerts = []
    else:
        _alerts = []


def _save():
    os.makedirs(os.path.dirname(_ALERT_FILE), exist_ok=True)
    with open(_ALERT_FILE, 'w', encoding='utf-8') as f:
        json.dump(_alerts, f, ensure_ascii=False, indent=2)


def add_alert(symbol, name, price, direction):
    """添加预警。direction: 'up'=上穿(涨穿), 'down'=下穿(跌穿)"""
    with _lock:
        alert = {
            'id': str(int(time.time() * 1000)),
            'symbol': symbol,
            'name': name,
            'price': float(price),
            'direction': direction,  # up / down
            'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'triggered': False,
            'triggered_at': None,
            'triggered_price': None
        }
        _alerts.append(alert)
        _save()
        return alert


def update_alert(alert_id, price=None, direction=None):
    """更新预警价格或方向"""
    with _lock:
        for a in _alerts:
            if a['id'] == alert_id:
                if price is not None:
                    a['price'] = float(price)
                if direction is not None:
                    a['direction'] = direction
                _save()
                return a
        return None


def remove_alert(alert_id):
    """删除单个预警"""
    with _lock:
        global _alerts
        _alerts = [a for a in _alerts if a['id'] != alert_id]
        _save()


def clear_triggered():
    """清空所有已触发的预警"""
    with _lock:
        global _alerts
        _alerts = [a for a in _alerts if not a['triggered']]
        _save()


def clear_all():
    """清空所有预警"""
    with _lock:
        global _alerts
        _alerts = []
        _save()


def get_alerts():
    """获取所有预警"""
    with _lock:
        return list(_alerts)


def get_active_alerts():
    """获取未触发的预警"""
    with _lock:
        return [a for a in _alerts if not a['triggered']]


def get_triggered_alerts():
    """获取已触发的预警"""
    with _lock:
        return [a for a in _alerts if a['triggered']]


def register_callback(cb):
    """注册预警触发回调函数"""
    _callbacks.append(cb)


def check_prices(prices_dict):
    """
    检查所有预警是否触发。
    prices_dict: {symbol: current_price}
    返回本次新触发的预警列表。
    """
    triggered_now = []
    with _lock:
        for alert in _alerts:
            if alert['triggered']:
                continue
            sym = alert['symbol']
            if sym not in prices_dict:
                continue
            cur = prices_dict[sym]
            last = _last_prices.get(sym)
            if last is None:
                # 第一次记录价格，不触发
                _last_prices[sym] = cur
                continue
            # 判断是否穿越
            hit = False
            if alert['direction'] == 'up' and last < alert['price'] <= cur:
                hit = True
            elif alert['direction'] == 'down' and last > alert['price'] >= cur:
                hit = True
            if hit:
                alert['triggered'] = True
                alert['triggered_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                alert['triggered_price'] = cur
                triggered_now.append(alert)
            _last_prices[sym] = cur
        if triggered_now:
            _save()
    # 触发回调
    for alert in triggered_now:
        for cb in _callbacks:
            try:
                cb(alert)
            except Exception as e:
                print(f'[alert] callback error: {e}')
    return triggered_now


# 初始化加载
_load()
