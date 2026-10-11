# -*- coding: utf-8 -*-
"""
缠论多品种预警 · 本地双向服务
- 一个端口(8000)：
    /pc            电脑总览仪表盘（17品种状态+统计）
    /pc/detail     电脑单品种详情（?sym=MA2610）
    /m             手机预警列表
    /m/detail      手机单品种详情（?sym=MA2610）
    /ws            WebSocket 双向频道（带品种信号/消息）
- 后台每 15 分钟刷新全部品种，任一品种出现新分型买卖点即推送给所有在线设备
运行：py server.py   （或双击 启动预警服务.bat）
"""
import sys, os, time, asyncio, datetime as dt, socket, json, base64, threading
import requests
from collections import deque
from contextlib import asynccontextmanager

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'pylibs'))
sys.path.insert(0, _HERE)

# 屏蔽系统Anaconda里与pylibs numpy 2.x冲突的可选依赖（pyarrow/numexpr/bottleneck）
# 这些都是pandas的可选性能优化库，不影响核心功能
import types as _types
for _mod in ['pyarrow', 'pyarrow.lib', 'pyarrow.compute', 'numexpr', 'numexpr.interpreter', 'bottleneck', 'bottleneck.move']:
    if _mod not in sys.modules:
        _m = _types.ModuleType(_mod)
        setattr(_m, '__version__', '0.0.0')  # 动态挂属性，避免类型检查器误报
        sys.modules[_mod] = _m

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse

import engine, fees, backtest, trading_time, config, alert_manager
from chanlun.pattern import fingerprint as pattern_fingerprint

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web')
PORT = 8000
HISTORY_KEEP = 60


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return '127.0.0.1'
    finally:
        s.close()


class Manager:
    def __init__(self):
        self.conns = []
        self.history = []
        self._seq = 0

    def devices(self):
        return [{'role': c['role'], 'name': c['name']} for c in self.conns]

    async def send(self, ws, msg):
        try:
            await ws.send_json(msg)
        except Exception:
            pass

    async def broadcast(self, msg, exclude=None):
        for c in list(self.conns):
            if c['ws'] is exclude:
                continue
            await self.send(c['ws'], msg)

    def remember(self, msg):
        self.history.append(msg)
        if len(self.history) > HISTORY_KEEP:
            self.history = self.history[-HISTORY_KEEP:]

    def remember_replace(self, msg):
        """同指纹的历史卡片原位更新（事件流延续触发时，保持首次推送时间不变）。"""
        fp = msg.get('fingerprint')
        for i, old in enumerate(self.history):
            if old.get('fingerprint') == fp:
                self.history[i] = msg
                return
        self.remember(msg)

    async def connect(self, ws, role):
        await ws.accept()
        self._seq += 1
        name = ('电脑端' if role == 'pc' else '手机端') + f"#{self._seq}"
        conn = {'ws': ws, 'role': role, 'name': name}
        self.conns.append(conn)
        await self.send(ws, {'type': 'init', 'role': role, 'name': name,
                             'devices': self.devices(),
                             'history': self.history[-30:]})
        await self.broadcast({'type': 'devices', 'devices': self.devices(),
                              'ts': time.time()}, exclude=ws)
        return conn

    def disconnect(self, conn):
        if conn in self.conns:
            self.conns.remove(conn)


manager = Manager()


async def do_refresh(broadcast=True):
    """刷新全部品种总览（页面数据靠它）。
    旧"分型买卖点 signal 推送"已停用（用户指示：被形态预警系统取代，设计文档 §5.5）。"""
    if broadcast:
        await manager.broadcast({'type': 'status', 'text': '正在刷新全部品种行情…', 'ts': time.time()})
    ov, _new_sigs = await engine.refresh_and_detect_all_async()
    if broadcast:
        await manager.broadcast({'type': 'overview', 'overview': ov, 'ts': time.time()})
    return ov, []


async def periodic():
    # 启动先拉一次并记录各品种信号基线（不把历史信号当新预警），完成后广播总览
    try:
        ov, _ = await engine.refresh_and_detect_all_async()
        await manager.broadcast({'type': 'overview', 'overview': ov, 'ts': time.time()})
    except Exception as e:
        print('initial refresh error:', e)
    while True:
        now = dt.datetime.now()
        nxt = now.replace(minute=(now.minute // 15) * 15, second=20, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(minutes=15)
        await asyncio.sleep(max(5, (nxt - now).total_seconds()))
        try:
            if trading_time.is_trading_time():
                await do_refresh(broadcast=True)
            else:
                st = trading_time.trading_status()
                print(f'[periodic] {dt.datetime.now().strftime("%H:%M")} {st["reason"]}，跳过15分刷新')
        except Exception as e:
            print('periodic error:', e)


async def alert_loop():
    await asyncio.sleep(30)
    while True:
        try:
            if not trading_time.is_trading_time():
                await asyncio.sleep(10)
                continue
            loop = asyncio.get_event_loop()
            import tqsdk_data as tq
            syms = sorted({a['symbol'] for a in alert_manager.get_active_alerts()}
                          | {c['symbol'] for c in config.CONTRACTS})
            prices = {}
            def _one(s):
                try:
                    df = tq.fetch_kline(s, 30, data_length=2)
                    if df is not None and len(df):
                        return float(df['close'].iloc[-1])
                except Exception:
                    pass
                return None
            for s in syms:
                p = _one(s)
                if p is not None:
                    prices[s] = p
            triggered = alert_manager.check_prices(prices)
            for a in triggered:
                msg = {'type': 'price_alert', 'id': a['id'], 'symbol': a['symbol'],
                       'name': a['name'], 'price': a['price'], 'direction': a['direction'],
                       'triggered_price': a['triggered_price'], 'triggered_at': a['triggered_at'],
                       'ts': time.time()}
                await manager.broadcast(msg)
        except Exception as e:
            print('[alert_loop] error:', e)
        await asyncio.sleep(30)


# ==================== 背驰-V反 形态预警系统（docs/背驰反转形态预警系统设计.md §5/§9） ====================

_PATTERN_LABEL = {'30': '30秒', '1': '1分', '3': '3分', '5': '5分', '15': '15分',
                  '30m': '30分', '60': '60分', 'daily': '日线'}
_CELL_RANK = {'conf_a': 5, 'trig_b': 4, 'cand_a': 3, 'cand_b': 2, 'watch_a': 1, 'gone_b': 0, 'idle': 0}
_pattern_cells = {}      # (symbol, period) -> cell dict（矩阵页全量状态）
_pattern_reported = {}   # (symbol, period) -> set(结构指纹)，进程内去重
_pattern_events = deque(maxlen=100)  # 事件流全部历史（客户端按页签裁：全部30/确认20）
_pattern_ready = False   # 首轮静默：只建基线不推送（§5.2）
_snapshot_done = False   # 启动机会快照只推一次
_snapshot_msgs = []      # 静默基线扫描期间收集的新鲜事件（快照用）

# 笔六开关：监控跟随详情页⚙面板（localStorage cl_bi_conf），进矩阵页时前端同步过来。
# 变更后静默重扫全量（~18s/85单元）并重推机会快照；调试期频繁改动也只在实际进页面时才触发。
_pattern_bi_conf = dict(engine.BI_CONF_DEFAULT)


def _coerce_bi_conf(raw):
    """前端 biConf（字符串值，如 '1'/'0'/'normal'/'strict'）→ 引擎笔配置（正确类型）。"""
    conf = dict(engine.BI_CONF_DEFAULT)
    if not isinstance(raw, dict):
        return conf
    for k in conf:
        if raw.get(k) is None:
            continue
        v = raw[k]
        if isinstance(conf[k], bool):
            conf[k] = str(v).lower() in ('1', 'true')
        else:
            conf[k] = str(v)
    return conf


def _push_rescan_task():
    async def _rescan():
        global _pattern_ready, _snapshot_done
        _pattern_ready = False          # 静默重建基线（新配置下历史指纹全部作废）
        _snapshot_done = False
        await _pattern_scan_once(False)
        _pattern_ready = True
        await _push_startup_snapshot()  # 重推当前机会快照
    return asyncio.ensure_future(_rescan())


async def _pattern_scan_once(push, periods=None):
    global _snapshot_msgs
    if not push:
        _snapshot_msgs = []   # 每轮静默基线扫描重新收集
    periods = periods or config.PATTERN_PERIODS
    for c in config.CONTRACTS:
        sym, name = c['symbol'], c['name']
        for period in periods:
            key = (sym, period)
            rep = _pattern_reported.setdefault(key, set())
            try:
                # 线程池里跑（fetch+算笔是同步阻塞），避免卡住事件循环
                snap = await asyncio.get_event_loop().run_in_executor(
                    None, engine.pattern_snapshot, sym, period, rep, _pattern_bi_conf)
            except Exception:
                continue   # 掐线重连窗口/空数据 → 跳过该单元本轮，下轮自然恢复
            try:
                await _apply_pattern(sym, name, period, snap['result'], push)
            except Exception as e:
                print(f'[pattern] apply {sym} {period} error:', e)
    _fade_pattern_cells()


def _pattern_brief(pattern, ev):
    """标准缠论话术（不含品种/级别前缀），一个关键价格。
    用户术语：A类=背驰反转（候选=突破 / 确认=回调笔确立，即二买进场关注）；
    B类=三买卖点（候选=突破+幅度过滤 / 触发=回调笔成立未碰5，即进场关注）。"""
    p = pattern
    if p == 'VREV_LONG':
        return f"突破 <b>{ev['p2']:g}</b> · A类候选（等回调确认二买）"
    if p == 'VREV_SHORT':
        return f"跌破 <b>{ev['p2']:g}</b> · A类候选（等反抽确认二卖）"
    if p == 'VREV_CONF_LONG':
        return f"回调笔确立 <b>{ev['price']:g}</b> · A类确认（二买进场关注）"
    if p == 'VREV_CONF_SHORT':
        return f"回调笔确立 <b>{ev['price']:g}</b> · A类确认（二卖进场关注）"
    if p == '3BUY_CAND':
        return f"三买候选 · 回踩不破 <b>{ev['p1']:g}</b> 确认（幅度 {ev.get('ratio', '?')}×）"
    if p == '3SELL_CAND':
        return f"三卖候选 · 反抽不破 <b>{ev['p1']:g}</b> 确认（幅度 {ev.get('ratio', '?')}×）"
    if p == '3BUY_TRIG':
        return f"三买触发 · 回踩不破 <b>{ev['p1']:g}</b>（进场关注）"
    if p == '3SELL_TRIG':
        return f"三卖触发 · 反抽不破 <b>{ev['p1']:g}</b>（进场关注）"
    return f"{p} @ {ev.get('price', ''):g}"


def _pattern_text(name, period, ev):
    return f"{name} {_PATTERN_LABEL.get(period, period)} {_pattern_brief(ev['pattern'], ev)}"


def _fmt_time_str(time_val):
    """ISO 时间字符串 → 'MM-DD HH:MM'（前端直接显示，避免各页 fmtTime 兼容问题）。"""
    try:
        t = dt.datetime.fromisoformat(str(time_val))
        return t.strftime('%m-%d %H:%M')
    except Exception:
        return ''


def _trading_elapsed_seconds(t0, now):
    """两个时间点之间累计的**交易时段**秒数：跨节假日/周末不计（用户要求：
    24h 必须是交易日的 24h——9/30 的信号假期期间一直有效，直到开市后累计满 24h 交易时长）。"""
    import trading_time as tt
    # 统一为北京时间墙上时刻（交易时段表都是 naive 本地时间）
    if t0.tzinfo is not None:
        t0 = t0.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    if (now - t0).total_seconds() > 45 * 86400:   # 超过45个自然日必为历史
        return float('inf')
    total = 0.0
    d = t0.date()
    days = 0
    while d <= now.date() and days < 45:
        for sh, sm, eh, em in tt.SESSIONS:
            # 时段归属：凌晨段(00:00-02:35)属于前一交易日的夜盘，看前一自然日是否交易日
            if (sh, sm) == (0, 0):
                trade_ok = tt.is_trading_day(d - dt.timedelta(days=1))
            else:
                trade_ok = tt.is_trading_day(d)
            if not trade_ok:
                continue
            seg_start = dt.datetime.combine(d, dt.time(sh, sm))
            seg_end = dt.datetime.combine(d, dt.time(eh, em))
            ov = (min(seg_end, now) - max(seg_start, t0)).total_seconds()
            if ov > 0:
                total += ov
        d += dt.timedelta(days=1)
        days += 1
    return total


def _is_stale_structure(time_str):
    """【仅用于推送区/事件流】累计交易时长超过 PATTERN_CELL_TTL（24h 交易时长 ≈ 2.6 个交易日）的推送视为历史。
    格子（矩阵）不用此函数——格子上永远实时显示状态机的当前阶段，无任何时间过滤。
    节假日/周末/夜盘闭市时间不计入（解析失败按新鲜处理）。"""
    try:
        t = dt.datetime.fromisoformat(str(time_str))
        if t.tzinfo is None:
            t = t.astimezone()
        now = dt.datetime.now().astimezone()
        return _trading_elapsed_seconds(t, now) > config.PATTERN_CELL_TTL
    except Exception:
        return False


def _pick_active(result):
    """从多空两侧评估结果里挑矩阵格子该显示的状态（优先级 > 时间新）。
    格子=实时状态：观察/候选/确认（A）与候选/触发（B）都是真实阶段，无任何时间过滤
    （阶段终止只由状态机规则决定：破背驰点作废/离开确认分型区域/结构重建）。
    _is_stale_structure 仅用于推送区/事件流（§5.5），不要在这里调用。"""
    best = None
    for side in ('long', 'short'):
        st = result[side]
        if st['state'] == 'idle' or not st['struct']:
            continue
        s = st['struct']
        item = {'state': st['state'], 'side': side, 'struct': s,
                'price': s['p3'], 'time': s['t3'], 'pattern': None, 'fp': None}
        if st['state'] == 'cand_a':
            item['pattern'] = 'VREV_LONG' if side == 'long' else 'VREV_SHORT'
        elif st['state'] == 'conf_a':
            item['pattern'] = 'VREV_CONF_LONG' if side == 'long' else 'VREV_CONF_SHORT'
        elif st['state'] == 'cand_b':
            item['pattern'] = '3BUY_CAND' if side == 'long' else '3SELL_CAND'
        elif st['state'] == 'trig_b':
            item['pattern'] = '3BUY_TRIG' if side == 'long' else '3SELL_TRIG'
        if st['fired']:
            ev = st['fired'][-1]   # 最新事件为准（价格/时间/指纹）
            item.update({'pattern': ev['pattern'], 'price': ev['price'],
                         'time': ev['time'], 'fp': ev['fingerprint'], 'ratio': ev.get('ratio')})
        else:
            if item['pattern']:
                item['fp'] = pattern_fingerprint(item['pattern'], s)
        key = (_CELL_RANK[st['state']], str(s.get('t3', '')))
        if best is None or key > best[0]:
            best = (key, item)
    return best[1] if best else None


async def _upsert_event_card(msg):
    """事件卡片：同指纹已存在 → 原位更新（保留首次时间，时间线刷新，广播 update 类型）；
    不存在 → 新建。端点没变化不广播（进行中笔每轮重评会重复产出同一事件）。
    用户定稿 2026-10-08：候选/确认无优先级、纯时间序；延续触发只更新卡片不另开新卡。"""
    fp = msg.get('fingerprint')
    dup_idx = [i for i, old in enumerate(_pattern_events) if old.get('fingerprint') == fp]
    if dup_idx:
        # 历史遗留：多级别单元同指纹曾各自入队（快照收集期不去重所致），清掉多余条目只留第一个
        for j in sorted(dup_idx[1:], reverse=True):
            del _pattern_events[j]
        old = _pattern_events[dup_idx[0]]
        if old.get('time') == msg.get('time') and old.get('price') == msg.get('price'):
            return                     # 端点没变，无需更新
        msg['first_time'] = old.get('first_time') or old.get('time')
        msg['type'] = 'pattern_alert_update'
        _pattern_events[dup_idx[0]] = msg
        manager.remember_replace(msg)
        await manager.broadcast(msg)
        return
    msg['first_time'] = msg.get('time')
    _pattern_events.append(msg)
    manager.remember(msg)
    await manager.broadcast(msg)


async def _apply_pattern(sym, name, period, result, push):
    key = (sym, period)
    old = _pattern_cells.get(key)
    now = time.time()

    # —— 1) fired 事件 → pattern_alert 消息（§5.3）——
    _psec = engine._TQ_PERIOD.get(period, 900)
    for side in ('long', 'short'):
        for ev in result[side]['fired']:
            if _is_stale_structure(ev.get('time')):
                continue   # 超过24h交易时长的历史事件不进推送区
            # 触发时刻 = 端点K线的完结时刻（开始时刻+周期）——用户定稿：排序/展示
            # 都用触发时刻，不用K线开始时刻（60分K线21:00开始→22:00才触发→标22:00）
            try:
                _trig = str(dt.datetime.fromisoformat(str(ev['time'])) + dt.timedelta(seconds=_psec))
            except Exception:
                _trig = str(ev.get('time') or '')
            msg = {'type': 'pattern_alert', 'pattern': ev['pattern'],
                   'kind': 'A' if ev['pattern'].startswith('VREV') else 'B',
                   'side': 'long' if 'LONG' in ev['pattern'] or 'BUY' in ev['pattern'] else 'short',
                   'symbol': sym, 'name': name, 'period': period,
                   'price': ev['price'], 'time': ev['time'], 'trigger_time': _trig,
                   'time_str': _fmt_time_str(ev['time']),
                   'p1': ev['p1'], 'p2': ev['p2'], 'p3': ev['p3'],
                   'bi_span': ev.get('bi_span'), 'ratio': ev.get('ratio'),
                   'brief': _pattern_brief(ev['pattern'], ev),
                   'fingerprint': ev['fingerprint'],
                   'text': _pattern_text(name, period, ev), 'ts': time.time(),
                   'first_time': ev['time']}   # 快照重建时首检时刻≈触发时刻（内存流不丢首次显示）
            if push:
                await _upsert_event_card(msg)
            else:
                _snapshot_msgs.append(msg)   # 基线期收集，首轮扫描完成后由快照统一推送

    # —— 2) 格子状态推导（§9.3 消退/钉住规则；历史告警在 _pick_active 内已过滤）——
    act = _pick_active(result)
    new_state = act['state'] if act else 'idle'
    if old and old['state'] == 'cand_b' and new_state == 'idle':
        new_state, act = 'gone_b', None          # B 类候选作废：短暂显示
    if old and old['state'] == 'gone_b' and new_state == 'gone_b' and now - old['ts'] > 600:
        new_state, act = 'idle', None            # 10 分钟后自动回 idle

    cell = {'symbol': sym, 'name': name, 'period': period, 'state': new_state,
            'side': act['side'] if act else None, 'ts': now}
    if act:
        s = act['struct']
        cell.update({'pattern': act.get('pattern'), 'price': act.get('price'),
                     'time': act.get('time'), 'p1': s['p1'], 'p2': s['p2'], 'p3': s['p3'],
                     'fp': act.get('fp'), 'ratio': act.get('ratio')})
    if old and old['state'] == new_state and old.get('side') == cell.get('side') \
            and old.get('fp') == cell.get('fp') and old.get('price') == cell.get('price'):
        return                                    # 无变化不广播
    _pattern_cells[key] = cell
    await manager.broadcast({'type': 'pattern_state', 'cell': cell, 'ts': time.time()})


def _fade_pattern_cells():
    """24h 自动淡化 fired/conf；gone_b 10 分钟后回 idle（§9.3）。"""
    now = time.time()
    for key, cell in list(_pattern_cells.items()):
        expire = {'gone_b': 600}.get(cell['state'])   # 格子上所有状态实时显示，唯一例外=作废态短暂过渡
        if expire and now - cell['ts'] > expire:
            cell = {'symbol': cell['symbol'], 'name': cell['name'], 'period': cell['period'],
                    'state': 'idle', 'side': None, 'ts': now}
            _pattern_cells[key] = cell
            asyncio.ensure_future(manager.broadcast({'type': 'pattern_state', 'cell': cell, 'ts': now}))


async def _push_startup_snapshot():
    """静默基线扫描收集到的新鲜事件（候选+确认，均已在24h交易时长内）按时间升序统一推送。
    不做候选/确认取舍——事件流全量入 deque(100)，矩阵页按页签裁剪（全部30/确认20）。"""
    global _snapshot_done, _snapshot_msgs
    if _snapshot_done:
        return
    _snapshot_done = True
    # 按触发时刻升序（前端再按触发时刻排序展示，新事件置顶）
    _snapshot_msgs.sort(key=lambda m: str(m.get('trigger_time') or m.get('time') or ''))
    seen = {}
    for msg in _snapshot_msgs:
        # 同指纹去重：指纹=pattern|p1|p2 不含级别，同一结构在多个级别单元都会 fire，只留触发时刻最新的
        fp = msg.get('fingerprint')
        old = seen.get(fp)
        if old is None or str(msg.get('trigger_time') or msg.get('time') or '') > str(old.get('trigger_time') or old.get('time') or ''):
            seen[fp] = msg
    for msg in seen.values():
        if any(m.get('fingerprint') == msg.get('fingerprint') for m in _pattern_events):
            continue   # 事件流已有同指纹卡片，不重复入队
        _pattern_events.append(msg)
        manager.remember(msg)
        await manager.broadcast(msg)
    _snapshot_msgs = []


# 各级别扫描间隔（分钟）：按挂钟对齐（当天第 N 分钟 % 间隔 == 0 才拉该级别），
# 重启不会累积计数；15 分级别在 :00/:15/:30/:45 拉，与 3/5 的对齐点重合时同一轮内各自只拉一次
_PERIOD_SCAN_MIN = {'3': 3, '5': 5, '15': 15, '30m': 30, '60': 60}
_last_scan_minute = {}    # period -> 上次扫描的 minute_of_day（防同一分钟内 30s tick 重复拉）


_SESSION_CLOSE_MIN = (11 * 60 + 30, 15 * 60, 23 * 60, 2 * 60 + 30)   # 各时段真实收盘（分钟）
_last_catchup = None       # (日期, 收盘分钟) 防同一次收盘重复收尾
_baseline_ts = 0.0         # 基线完成时刻（刚跑完基线就无需收尾，数据相同）
_last_loop_ts = None       # 上一轮循环时刻（检测停摆：合盖休眠超 4 分钟 → 恢复后立即补扫）


async def pattern_loop():
    """形态监视主循环（§5.2/§5.4 + 用户节奏要求）：
    交易时段每 30s 醒来检查挂钟——3 分级别每 3 分钟、5 分每 5 分钟、15/30/60 各按自己的
    分钟倍数对齐拉取（重合时刻同一序列只拉一次）。
    时段收尾扫描（用户定稿 2026-10-11）：收盘 3 分钟后补扫一轮全级别——各品种"收盘最后一根
    K 线"的数据到达时序有竞争，23:00 整收线的单元可能在对齐扫描轮漏发（白糖60 事件），收尾补齐。
    窗口 [收盘+3min, 收盘+5min)，落在 trading_time 的 5 分钟缓冲内（11:35/15:05/23:59/02:35）。
    节假日/非交易时段：只在启动后拉一遍基线（矩阵有数据可看），之后完全停止拉取，
    每分钟只检查一次是否进入交易时段——开市自动恢复，期间零网络请求。
    停摆恢复（用户定稿 2026-10-11）：循环空闲间隔超 4 分钟（合盖休眠/系统挂起，正常只睡
    30s/60s）→ 恢复后立即补扫全级别，不等各自主对齐点（否则 3 分最多等 3 分钟、
    30 分要等 30 分钟、60 分等 1 小时）。阈值 4 分钟 > 最小级别 3 分钟，短断片由各级别
    自己的对齐点自愈，不触发全扫。"""
    global _pattern_ready, _last_catchup, _baseline_ts, _last_loop_ts
    await asyncio.sleep(15)   # 等服务与其他初始化就绪
    while True:
        try:
            now = dt.datetime.now()
            gap = _last_loop_ts is not None and (now - _last_loop_ts).total_seconds() > 240
            if trading_time.is_trading_time():
                if not _pattern_ready:
                    # 启动基线：全级别立即扫一遍（否则 60 分级别要等下一个整点，矩阵空窗太长），
                    # 之后进入挂钟对齐节奏；基线静默不推送
                    await _pattern_scan_once(False)
                    _pattern_ready = True
                    _baseline_ts = time.time()
                    await _push_startup_snapshot()
                    print(f'[pattern_loop] {dt.datetime.now():%H:%M} 启动基线扫描完成，进入挂钟对齐节奏')
                elif gap:
                    print(f'[pattern_loop] {now:%H:%M} 停摆恢复（间隔超 2 分钟），立即补扫全级别')
                    await _pattern_scan_once(True)
                    # 标记本轮已扫过的级别，避免紧跟的对齐点重复拉同一轮
                    for period in config.PATTERN_PERIODS:
                        iv = _PERIOD_SCAN_MIN.get(period)
                        if iv and now.minute % iv == 0:
                            _last_scan_minute[period] = now.hour * 60 + now.minute
                else:
                    # —— 时段收尾扫描：收盘 +3 分钟补一轮全级别，补齐"最后一根K线"漏发 ——
                    # 刚跑完基线（5 分钟内）则跳过：数据相同，补扫是冗余
                    nmin = now.hour * 60 + now.minute
                    for c in _SESSION_CLOSE_MIN:
                        if c + 3 <= nmin < c + 5 and _last_catchup != (now.date().isoformat(), c) \
                                and time.time() - _baseline_ts > 300:
                            _last_catchup = (now.date().isoformat(), c)
                            print(f'[pattern_loop] {now:%H:%M} 时段收尾扫描（{c//60:02d}:{c%60:02d} 收盘补齐，全级别）')
                            await _pattern_scan_once(True)
                            break
                    mod = now.hour * 60 + now.minute
                    due = []
                    for period in config.PATTERN_PERIODS:
                        iv = _PERIOD_SCAN_MIN.get(period)
                        if iv and mod % iv == 0 and _last_scan_minute.get(period) != mod:
                            _last_scan_minute[period] = mod
                            due.append(period)
                    if due:
                        await _pattern_scan_once(True, periods=due)
                await asyncio.sleep(30)
            else:
                # 节假日/非交易时段：启动后只拉一遍基线（矩阵有数据可看），之后零拉取
                if not _pattern_ready:
                    await _pattern_scan_once(False)
                    _pattern_ready = True
                    _baseline_ts = time.time()
                    await _push_startup_snapshot()
                    print(f'[pattern_loop] {dt.datetime.now():%H:%M} 非交易时段基线就绪，'
                          f'停止定时拉取，等待开市自动恢复')
                elif gap:
                    # 停摆横跨收盘/休市（如 22:50 合盖 23:05 打开）：补扫一轮，
                    # 覆盖休眠期间收盘的最后K线
                    print(f'[pattern_loop] {now:%H:%M} 停摆恢复（非交易时段），补扫一轮全级别')
                    await _pattern_scan_once(True)
                await asyncio.sleep(60)   # 非交易时段每分钟只查一次是否开市（不发任何数据请求）
            _last_loop_ts = dt.datetime.now()   # 一轮工作全部完成才更新——gap 度量空闲间隔，
            # 耗时长的扫描不会误判成停摆（否则会级联触发一轮又一轮全扫）
        except Exception as e:
            print('[pattern_loop] error:', e)
            await asyncio.sleep(30)


async def fees_refresh_loop():
    """启动 3 分钟后拉一次手续费/保证金（用户定稿 2026-10-10：每次启动都刷新，
    特殊行情交易所调费率快；避开启动重活——首次行情基线+形态基线扫描那时已完成，
    akshare 拉取在线程池里跑，不卡事件循环；失败沿用旧缓存）。"""
    await asyncio.sleep(180)
    try:
        await asyncio.get_event_loop().run_in_executor(None, lambda: fees.load(force=True))
        print('[fees] 启动刷新完成（手续费/保证金）')
    except Exception as e:
        print('[fees] 启动刷新失败（沿用旧缓存）：', e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(periodic())
    alert_task = asyncio.create_task(alert_loop())
    pattern_task = asyncio.create_task(pattern_loop())
    fees_task = asyncio.create_task(fees_refresh_loop())
    yield
    task.cancel()
    alert_task.cancel()
    pattern_task.cancel()
    fees_task.cancel()


app = FastAPI(lifespan=lifespan)


def _page(name):
    """HTML 页面统一禁缓存：改版后浏览器普通刷新即拿到新版（用户曾因缓存看到旧页面）。"""
    return FileResponse(os.path.join(WEB_DIR, name), headers={'Cache-Control': 'no-cache'})


@app.get('/')
async def index():
    return RedirectResponse('/pc')


@app.get('/pc')
async def page_pc_overview():
    return _page('pc_overview.html')


@app.get('/pc/detail')
async def page_pc_detail():
    return _page('pc_detail.html')


@app.get('/m')
async def page_m_list():
    return _page('m_list.html')


@app.get('/m/detail')
async def page_m_detail():
    return _page('m_detail.html')


@app.get('/pc/backtest')
async def page_backtest():
    return _page('pc_backtest.html')


@app.get('/pc/help')
async def page_pc_help():
    return _page('pc_help.html')


@app.get('/pc/matrix')
async def page_pc_matrix():
    """全级别并行监控矩阵（设计文档 §9，用户主力常驻页面）。"""
    return _page('pc_matrix.html')


@app.get('/api/pattern_matrix')
async def api_pattern_matrix():
    """矩阵页全量状态：cells + 事件流 + 行列定义（行列由 config 驱动，改 config 页面零改动）。
    ready=启动基线+快照推送是否已完成（测试/前端可据此等待流稳定）。"""
    return JSONResponse({
        'cells': list(_pattern_cells.values()),
        'events': list(_pattern_events),
        'periods': list(reversed(config.PATTERN_PERIODS)),   # 倒序：日线在最上（加上后）
        'contracts': config.CONTRACTS,
        'ready': _snapshot_done,
        'ts': time.time(),
    })


@app.post('/api/pattern_bi_conf')
async def api_pattern_bi_conf(request: Request):
    """同步详情页⚙面板的笔六开关到监控系统（矩阵页进入时前端 POST localStorage cl_bi_conf）。
    配置有变化时：清空基线 → 静默重扫全量（85单元约18秒）→ 重推当前机会快照。"""
    global _pattern_bi_conf
    try:
        raw = await request.json()
    except Exception:
        raw = None
    new_conf = _coerce_bi_conf(raw)
    old_json = json.dumps(_pattern_bi_conf, sort_keys=True)
    new_json = json.dumps(new_conf, sort_keys=True)
    if new_json == old_json:
        return JSONResponse({'ok': True, 'changed': False})
    _pattern_bi_conf = new_conf
    _pattern_cells.clear()
    _pattern_reported.clear()
    _push_rescan_task()
    return JSONResponse({'ok': True, 'changed': True, 'conf': new_conf})


# ==================== 持仓状态（监控页/详情页/手机端，2026-10-09） ====================
# 架构：Gitee 私有仓库 = 唯一权威数据源（positions.json=当前持仓, history.json=平仓简报）。
# 电脑端：本地 data/*.json 为缓存，写=本地立即生效+后台线程推 Gitee（失败重试）；
#         读=本地 + 限频拉取远端（手机端改了电脑能看到）。
# 手机端：直连 Gitee API 读写（CORS 已实测全放行），不依赖局域网。

_POSITIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'positions.json')
_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'history.json')
_POS_EXIT_MAP = {'二买': '二卖', '二卖': '二买', '三买': '三卖', '三卖': '三买'}
_POS_WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
_POS_LIMIT = 4
_POS_LOCK = threading.Lock()
_GITEE_SHA = {}                                          # remote_path -> 上次已同步 sha
_POS_DIRTY = {'positions.json': False, 'history.json': False}
_POS_LAST_ERR = ['']
_LAST_PULL = [0.0]
_SYNC_LOCK = threading.Lock()                            # 推送串行化（并发线程会互相用旧sha覆盖）


def _positions_load():
    try:
        with open(_POSITIONS_FILE, encoding='utf-8') as f:
            return json.load(f).get('positions', [])
    except Exception:
        return []


def _positions_save(positions):
    with open(_POSITIONS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'positions': positions}, f, ensure_ascii=False, indent=1)


def _history_load():
    try:
        with open(_HISTORY_FILE, encoding='utf-8') as f:
            return json.load(f).get('history', [])
    except Exception:
        return []


def _history_save(hist):
    with open(_HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump({'history': hist}, f, ensure_ascii=False, indent=1)


def _gitee_conf():
    try:
        with open(os.path.join(os.path.dirname(_POSITIONS_FILE), 'gitee_conf.json'), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _gitee_api(remote):
    conf = _gitee_conf()
    api = f"https://gitee.com/api/v5/repos/{conf.get('owner')}/{conf.get('repo')}/contents/{remote}"
    return api, conf


def _gitee_push_file(remote, local):
    """推单个文件（GET sha → PUT 内容）。返回 ''=成功，否则错误串。"""
    api, conf = _gitee_api(remote)
    token, branch = conf.get('token'), conf.get('branch', 'master')
    sha = _GITEE_SHA.get(remote)
    if sha is None:
        try:
            r = requests.get(api, params={'access_token': token, 'ref': branch}, timeout=10)
            if r.status_code == 200:
                d = r.json()
                sha = d.get('sha') if isinstance(d, dict) else None   # Gitee 对不存在文件偶发返回 []（视为新建）
            elif r.status_code != 404:
                return f'GET {r.status_code}'
        except Exception as e:
            return str(e)[:120]
    with open(local, 'rb') as f:
        content = base64.b64encode(f.read()).decode()
    body = {'access_token': token, 'content': content,
            'message': f'持仓同步 {dt.datetime.now().strftime("%m-%d %H:%M:%S")}', 'branch': branch}
    if sha:
        body['sha'] = sha
    try:
        r = requests.put(api, json=body, timeout=15)
        if r.status_code in (200, 201):
            _GITEE_SHA[remote] = (r.json().get('content') or {}).get('sha') or sha
            return ''
        if sha:
            _GITEE_SHA[remote] = None   # sha 冲突等：作废缓存，下轮重取再推
        return f'PUT {r.status_code}: {r.text[:150]}'
    except Exception as e:
        return str(e)[:120]


def _bg_sync():
    """后台推送线程：把脏文件推到 Gitee，整体重试 2 轮；仍失败保留脏标记，
    下次任意持仓 API 调用时再触发（自愈）。"""
    def run():
        with _SYNC_LOCK:                 # 串行：并发推送会互相用旧 sha 覆盖（实测教训）
            for _ in range(3):
                errs = []
                for remote, dirty in list(_POS_DIRTY.items()):
                    if not dirty:
                        continue
                    err = _gitee_push_file(remote, _POSITIONS_FILE if remote == 'positions.json' else _HISTORY_FILE)
                    if err:
                        errs.append(f'{remote}: {err}')
                    else:
                        _POS_DIRTY[remote] = False
                if not errs:
                    _POS_LAST_ERR[0] = ''
                    if not any(_POS_DIRTY.values()):
                        return           # 收尾复查：期间若有新变更则再跑一轮
                    continue
                err = '; '.join(errs)
                _POS_LAST_ERR[0] = err
                time.sleep(2)
            print(f'[持仓] Gitee 同步失败（将于下次操作时重试）: {err}')
    threading.Thread(target=run, daemon=True).start()


def _gitee_pull_if_stale():
    """限频拉取远端 positions（手机端改动回灌电脑端）；本地有未推变更时跳过。"""
    if _POS_DIRTY['positions.json'] or time.time() - _LAST_PULL[0] < 8:
        return
    _LAST_PULL[0] = time.time()
    conf = _gitee_conf()
    if not (conf.get('token') and conf.get('owner')):
        return
    remote = conf.get('remote_path', 'positions.json')
    api, _c = _gitee_api(remote)
    try:
        r = requests.get(api, params={'access_token': conf['token'], 'ref': conf.get('branch', 'master')}, timeout=8)
        if r.status_code != 200:
            return
        d = r.json()
        sha = d.get('sha')
        if not sha or sha == _GITEE_SHA.get(remote):
            return
        data = json.loads(base64.b64decode(d['content']).decode())
        with _POS_LOCK:
            _positions_save(data.get('positions', []))
        _GITEE_SHA[remote] = sha
    except Exception:
        pass


@app.get('/api/positions')
async def api_positions():
    _gitee_pull_if_stale()
    if any(_POS_DIRTY.values()):
        _bg_sync()   # 有未推成功的变更（上轮失败/被互斥跳过）→ 补推
    return JSONResponse({'positions': _positions_load(), 'limit': _POS_LIMIT,
                         'gitee_err': _POS_LAST_ERR[0], 'ts': time.time()})


@app.post('/api/positions/add')
async def api_positions_add(request: Request):
    p = await request.json()
    sym, period = str(p.get('symbol', '')), str(p.get('period', ''))
    entry = str(p.get('entry', ''))
    if sym not in config.CONTRACT_MAP:
        return JSONResponse({'error': f'未知品种 {sym}'}, status_code=400)
    if entry not in _POS_EXIT_MAP:
        return JSONResponse({'error': f'买卖点须为 {"、".join(_POS_EXIT_MAP)}'}, status_code=400)
    with _POS_LOCK:
        positions = _positions_load()
        if len(positions) >= _POS_LIMIT:
            return JSONResponse({'error': f'持仓已达上限（{_POS_LIMIT}个）'}, status_code=400)
        now = dt.datetime.now()
        rec = {'id': str(int(now.timestamp() * 1000)), 'symbol': sym,
               'name': config.CONTRACT_MAP[sym]['name'], 'period': period,
               'entry': entry, 'exit': str(p.get('exit') or _POS_EXIT_MAP[entry]),
               'open_time': now.strftime('%m-%d %H:%M'), 'weekday': _POS_WEEKDAYS[now.weekday()],
               'open_iso': now.isoformat(timespec='seconds'), 'note': str(p.get('note', '') or '')[:200],
               'result': '', 'pnl': ''}
        positions.append(rec)
        _positions_save(positions)
    _POS_DIRTY['positions.json'] = True
    _bg_sync()
    return JSONResponse({'ok': True, 'positions': positions})


@app.post('/api/positions/update')
async def api_positions_update(request: Request):
    p = await request.json()
    pid = str(p.get('id', ''))
    with _POS_LOCK:
        positions = _positions_load()
        rec = next((x for x in positions if x['id'] == pid), None)
        if rec is None:
            return JSONResponse({'error': '未找到该持仓'}, status_code=404)
        for k in ('entry', 'exit', 'note', 'result', 'pnl'):
            if k in p:
                rec[k] = str(p[k])[:200]
        if 'entry' in p:
            rec['exit'] = str(p.get('exit') or _POS_EXIT_MAP.get(rec['entry'], rec['entry']))
        _positions_save(positions)
    _POS_DIRTY['positions.json'] = True
    _bg_sync()
    return JSONResponse({'ok': True, 'positions': positions})


@app.post('/api/positions/delete')
async def api_positions_delete(request: Request):
    """平仓删除：记录移入 history.json（简报：止盈/止损/保本 + 盈亏，选填；
    自动补持仓时长/平仓时间/星期），positions 与 history 一起推 Gitee。"""
    p = await request.json()
    pid = str(p.get('id', ''))
    result = str(p.get('close_result', '') or '')
    pnl = str(p.get('close_pnl', '') or '')
    with _POS_LOCK:
        positions = _positions_load()
        rec = next((x for x in positions if x['id'] == pid), None)
        if rec is None:
            return JSONResponse({'error': '未找到该持仓'}, status_code=404)
        now = dt.datetime.now()
        try:
            opened = dt.datetime.fromisoformat(rec.get('open_iso') or now.isoformat())
        except Exception:
            opened = now
        # 空简报（结果和盈亏都空）= 试验单，纯删除不进 history
        hist_rec = None
        if result or pnl:
            hist_rec = {**rec, 'close_result': result, 'close_pnl': pnl,
                        'close_time': now.strftime('%m-%d %H:%M'),
                        'close_iso': now.isoformat(timespec='seconds'),
                        'weekday_close': _POS_WEEKDAYS[now.weekday()],
                        'hold_min': max(0, int((now - opened).total_seconds() // 60))}
            hist = _history_load()
            hist.insert(0, hist_rec)
            _history_save(hist)
        left = [x for x in positions if x['id'] != pid]
        _positions_save(left)
    if hist_rec:
        _POS_DIRTY['history.json'] = True
    _POS_DIRTY['positions.json'] = True
    _bg_sync()
    return JSONResponse({'ok': True, 'positions': left, 'closed': hist_rec})


@app.get('/api/positions/history')
async def api_positions_history():
    return JSONResponse({'history': _history_load()})


@app.get('/api/overview')
async def api_overview():
    return JSONResponse(engine.get_overview())


@app.get('/api/quotes')
async def api_quotes():
    """轻量行情：一次请求拉全部品种最新价/涨跌/保证金，不触碰K线与统计。"""
    loop = asyncio.get_event_loop()
    return JSONResponse(await loop.run_in_executor(None, engine.get_quotes))


@app.get('/api/detail')
async def api_detail(sym: str):
    return JSONResponse(engine.get_detail(sym))


@app.get('/api/fees')
async def api_fees():
    return JSONResponse(fees.load())


@app.get('/api/simulate')
async def api_simulate(sym: str, side: str = 'buy'):
    if side not in ('buy', 'sell'):
        side = 'buy'
    return JSONResponse(engine.simulate(sym, side))


@app.get('/api/backtest')
async def api_backtest(sym: str = 'ALL', months: int = 1):
    return JSONResponse(backtest.run_backtest(sym, months))


def _smart_markers(sym, period):
    """智能端点标记（用户定稿 2026-10-11）：只标当前有效结构上的买卖点，
    判不出的一律不显示（不回落成价格）。
    A类确认 → 2买/2卖（回调/反抽笔端点即买卖点位）；
    B类确认/触发 → 3买/3卖（回踩笔端点）；候选事件触发笔端点是突破极值、不是买卖点位，不标。
    同刻同向多标签合并（如 A类确认+B类确认同点位 → '2买+3买'）。"""
    labels = {}
    for ev in _pattern_events:
        if ev.get('symbol') != sym or ev.get('period') != period:
            continue
        p = str(ev.get('pattern') or '')
        if 'CONF' not in p and 'TRIG' not in p:
            continue   # 候选事件端点是突破极值，不是买卖点位
        if 'VREV' in p:
            lab, pos, color = ('2买', 'belowBar', '#ef5350') if 'LONG' in p else ('2卖', 'aboveBar', '#26a69a')
        elif '3BUY' in p:
            lab, pos, color = '3买', 'belowBar', '#ef5350'
        elif '3SELL' in p:
            lab, pos, color = '3卖', 'aboveBar', '#26a69a'
        else:
            continue
        try:
            # 与 engine.get_live 的 marker 时间基准一致：naive 北京时间按 UTC 取 epoch（前端 +8h 修正）
            ts = int(dt.datetime.fromisoformat(str(ev['time'])).replace(tzinfo=dt.timezone.utc).timestamp())
        except Exception:
            continue
        labels.setdefault((ts, pos), set()).add(lab)
    markers = [{'time': ts, 'position': pos, 'color': color, 'shape': 'circle',
                'text': '+'.join(sorted(labs))}
               for (ts, pos), labs in labels.items()
               for color in ['#ef5350' if pos == 'belowBar' else '#26a69a']]
    markers.sort(key=lambda m: m['time'])
    return markers[-30:]


@app.get('/api/live')
async def api_live(sym: str, period: str = '15', subpeak: str = '1',
                   algo: str = 'normal', strict: str = '1', fxcheck: str = 'strict',
                   endpeak: str = '1', gap: str = '0',
                   zsextend: str = '1', zsmerge: str = '0', zsfirst: str = '0',
                   mk: str = 'price', force: str = '0'):
    """看盘模式：指定周期K线+笔，独立于15分钟预警缓存。
    六个笔开关对应 chan.py CBiConfig，默认值=原版默认：
    algo(normal/fx) strict(1/0) fxcheck(loss/half/strict/totally)
    endpeak(1/0) subpeak(1/0) gap(1/0)
    三个中枢开关（边界处理参考 chan.py，默认=最标准缠论）：
    zsextend(1/0)中枢延伸 zsmerge(1/0)相邻中枢合并 zsfirst(1/0)允许首笔成枢"""
    if period not in ('30', '1', '3', '5', '15', '30m', '60', 'daily'):
        period = '15'
    if algo not in ('normal', 'fx'):
        algo = 'normal'
    if fxcheck not in ('loss', 'half', 'strict', 'totally'):
        fxcheck = 'strict'
    bi_conf = {
        'bi_algo': algo,
        'is_strict': strict != '0',
        'bi_fx_check': fxcheck,
        'bi_end_is_peak': endpeak != '0',
        'bi_allow_sub_peak': subpeak != '0',
        'gap_as_kl': gap == '1',
    }
    zs_conf = {
        'zs_extend': zsextend != '0',
        'zs_merge': zsmerge == '1',
        'zs_first_bi': zsfirst == '1',
    }
    try:
        s = engine.get_live(sym, period, bi_conf=bi_conf, zs_conf=zs_conf, force=force == '1')
        # 端点标记三档（⚙→📊 图表设置）：price=只有价格文字无圆点 / bs=原版箭头+买卖 / smart=2买3买2卖3卖（判不出不显示）
        if mk == 'bs':
            for m in s.get('markers') or []:
                m['text'] = '买' if m.get('position') == 'belowBar' else '卖'
                m['shape'] = 'arrowUp' if m.get('position') == 'belowBar' else 'arrowDown'
        elif mk == 'price':
            for m in s.get('markers') or []:
                m['size'] = 0   # 圆点缩为不可见，只留价格文字（marker 形状是必需项，库不支持无形状）
        elif mk == 'smart':
            s['markers'] = _smart_markers(sym, period)
        return JSONResponse(s)
    except Exception as e:
        return JSONResponse({'symbol': sym, 'error': str(e)[:120]}, status_code=200)
@app.get('/api/trading_status')
async def api_trading_status():
    """当前交易时段状态，供前端决定是否自动刷新。"""
    return JSONResponse(trading_time.trading_status())


@app.get('/api/contracts')
async def api_contracts():
    """盯盘合约清单（按 config.py 顺序），供详情页上下切换合约。"""
    return JSONResponse({'contracts': [{'symbol': c['symbol'], 'name': c['name']} for c in config.CONTRACTS]})


@app.get('/api/alerts')
async def api_get_alerts():
    return JSONResponse({'alerts': alert_manager.get_alerts()})


@app.get('/api/triggered_alerts')
async def api_get_triggered_alerts():
    return JSONResponse({'alerts': alert_manager.get_triggered_alerts()})


@app.post('/api/alerts')
async def api_add_alert(symbol: str, name: str, price: float, direction: str):
    a = alert_manager.add_alert(symbol, name, price, direction)
    return JSONResponse({'ok': True, 'alert': a})


@app.delete('/api/alerts/{alert_id}')
async def api_remove_alert(alert_id: str):
    alert_manager.remove_alert(alert_id)
    return JSONResponse({'ok': True})


@app.patch('/api/alerts/{alert_id}')
async def api_update_alert(alert_id: str, request: Request):
    try:
        body = await request.json()
    except:
        body = {}
    price = body.get('price')
    direction = body.get('direction')
    alert = alert_manager.update_alert(alert_id, price=price, direction=direction)
    if alert:
        return JSONResponse({'ok': True, 'alert': alert})
    return JSONResponse({'ok': False, 'error': 'not found'}, status_code=404)


@app.post('/api/alerts/clear_triggered')
async def api_clear_triggered():
    alert_manager.clear_triggered()
    return JSONResponse({'ok': True})


@app.post('/api/alerts/clear_all')
async def api_clear_all():
    alert_manager.clear_all()
    return JSONResponse({'ok': True})


# ===== 一键止损线（详情页 · 按现价反算金额/手数/点数）=====
# 存 JSON 文件（用户定稿 2026-10-10：像预警线一样随文件活，项目重启也在）。
# 每个品种一条通用线（不按级别分——止损位是交易的属性，拖动就是为了调整）。
_SL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'sl_lines.json')
_sl_lines = {}   # symbol -> {'price': float, 'ts': float}

def _sl_load():
    global _sl_lines
    try:
        with open(_SL_FILE, 'r', encoding='utf-8') as f:
            _sl_lines = json.load(f)
    except Exception:
        _sl_lines = {}

def _sl_save():
    try:
        os.makedirs(os.path.dirname(_SL_FILE), exist_ok=True)
        with open(_SL_FILE, 'w', encoding='utf-8') as f:
            json.dump(_sl_lines, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print('[sl_line] 写文件失败：', e)

_sl_load()


@app.get('/api/sl_line')
async def api_sl_get(sym: str):
    rec = _sl_lines.get(sym)
    return JSONResponse({'price': rec['price'] if rec else None})


@app.post('/api/sl_line')
async def api_sl_set(request: Request):
    try:
        d = await request.json()
        sym = str(d['sym'])
        price = float(d['price'])
    except Exception:
        return JSONResponse({'ok': False, 'error': 'bad params'}, status_code=400)
    _sl_lines[sym] = {'price': price, 'ts': time.time()}
    _sl_save()
    return JSONResponse({'ok': True})


@app.delete('/api/sl_line')
async def api_sl_del(sym: str):
    _sl_lines.pop(sym, None)
    _sl_save()
    return JSONResponse({'ok': True})


@app.websocket('/ws')
async def ws_endpoint(ws: WebSocket):
    role = ws.query_params.get('role', 'pc')
    if role not in ('pc', 'm'):
        role = 'pc'
    conn = await manager.connect(ws, role)
    try:
        while True:
            data = await ws.receive_json()
            t = data.get('type')
            if t in ('signal', 'chat', 'ack'):
                msg = dict(data)
                msg['from'] = role
                msg['ts'] = time.time()
                manager.remember(msg)
                await manager.broadcast(msg, exclude=ws)
            elif t == 'refresh_request':
                asyncio.create_task(do_refresh(broadcast=True))
            elif t == 'get_overview':
                ov = await asyncio.get_event_loop().run_in_executor(None, engine.get_overview)
                await manager.send(ws, {'type': 'overview', 'overview': ov, 'ts': time.time()})
    except WebSocketDisconnect:
        manager.disconnect(conn)
        await manager.broadcast({'type': 'devices', 'devices': manager.devices(), 'ts': time.time()})
    except Exception:
        manager.disconnect(conn)
        await manager.broadcast({'type': 'devices', 'devices': manager.devices(), 'ts': time.time()})


if __name__ == '__main__':
    ip = lan_ip()
    print('=' * 56)
    print('  缠论多品种预警服务已启动')
    print(f'  电脑总览： http://localhost:{PORT}/pc')
    print(f'  手机列表： http://{ip}:{PORT}/m   （手机连同一 WiFi）')
    print('  Ctrl+C 停止')
    print('=' * 56)
    # 预初始化天勤连接（后台线程，不阻塞启动）
    def _init_tq():
        try:
            import tqsdk_data as tq
        except Exception as e:
            print(f'[TqData] 导入数据源模块失败: {e}')
            return
        try:
            tq.TqData()._ensure_api()
            print('[TqData] 天勤连接预初始化完成')
        except tq.TqAuthError as e:
            # 致命认证错误：明确提示用户改 secret.py 后重启，不会静默重试
            print(f'[TqData] 预初始化失败（致命 - 不会自动重试）: {e}')
        except Exception as e:
            # 网络等瞬时错误：保留旧的"首次请求时会自动重试"提示
            print(f'[TqData] 预初始化失败（首次请求时会自动重试）: {e}')
    import threading as _th
    _th.Thread(target=_init_tq, daemon=True).start()
    uvicorn.run(app, host='0.0.0.0', port=PORT, log_level='warning')
