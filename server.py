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
import sys, os, time, asyncio, datetime as dt, socket, json
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
_pattern_events = deque(maxlen=20)  # 矩阵页底部实时事件流（卡片式，20条）
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
    格子=实时状态：观察/候选/确认（A）与候选/触发（B）都是真实阶段。
    确认/触发的效力只有 24h 交易时长（_is_stale_structure），过期的历史确认不上格子。"""
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


async def _apply_pattern(sym, name, period, result, push):
    key = (sym, period)
    old = _pattern_cells.get(key)
    now = time.time()

    # —— 1) fired 事件 → pattern_alert 消息（§5.3）——
    for side in ('long', 'short'):
        for ev in result[side]['fired']:
            if _is_stale_structure(ev.get('time')):
                continue   # 超过24h交易时长的历史事件不进推送区
            msg = {'type': 'pattern_alert', 'pattern': ev['pattern'],
                   'kind': 'A' if ev['pattern'].startswith('VREV') else 'B',
                   'side': 'long' if ev['pattern'].endswith('_LONG') or ev['pattern'].endswith('BUY') else 'short',
                   'symbol': sym, 'name': name, 'period': period,
                   'price': ev['price'], 'time': ev['time'],
                   'time_str': _fmt_time_str(ev['time']),
                   'p1': ev['p1'], 'p2': ev['p2'], 'p3': ev['p3'],
                   'bi_span': ev.get('bi_span'), 'ratio': ev.get('ratio'),
                   'brief': _pattern_brief(ev['pattern'], ev),
                   'text': _pattern_text(name, period, ev), 'ts': time.time()}
            if push:
                _pattern_events.append(msg)
                manager.remember(msg)
                await manager.broadcast(msg)
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
    """静默基线扫描收集到的新鲜事件（候选+确认，均已在24h交易时长内）统一推送到事件流。
    确认类优先、类内时间升序——保证确认不被候选挤出 20 条窗口。"""
    global _snapshot_done, _snapshot_msgs
    if _snapshot_done:
        return
    _snapshot_done = True
    _snapshot_msgs.sort(key=lambda m: (0 if 'CONF' in m['pattern'] or m['pattern'].endswith('TRIG') else 1,
                                       str(m.get('time') or '')))
    # 确认类优先入选 20 条窗口（假期积压确认过多时，最新的确认保留、候选让位）
    _snapshot_msgs = _snapshot_msgs[-_pattern_events.maxlen:]
    for msg in _snapshot_msgs:
        _pattern_events.append(msg)
        manager.remember(msg)
        await manager.broadcast(msg)
    _snapshot_msgs = []


# 各级别扫描间隔（分钟）：按挂钟对齐（当天第 N 分钟 % 间隔 == 0 才拉该级别），
# 重启不会累积计数；15 分级别在 :00/:15/:30/:45 拉，与 3/5 的对齐点重合时同一轮内各自只拉一次
_PERIOD_SCAN_MIN = {'3': 3, '5': 5, '15': 15, '30m': 30, '60': 60}
_last_scan_minute = {}    # period -> 上次扫描的 minute_of_day（防同一分钟内 30s tick 重复拉）


async def pattern_loop():
    """形态监视主循环（§5.2/§5.4 + 用户节奏要求）：
    交易时段每 30s 醒来检查挂钟——3 分级别每 3 分钟、5 分每 5 分钟、15/30/60 各按自己的
    分钟倍数对齐拉取（重合时刻同一序列只拉一次）。
    节假日/非交易时段：只在启动后拉一遍基线（矩阵有数据可看），之后完全停止拉取，
    每分钟只检查一次是否进入交易时段——开市自动恢复，期间零网络请求。"""
    global _pattern_ready
    await asyncio.sleep(15)   # 等服务与其他初始化就绪
    while True:
        try:
            if trading_time.is_trading_time():
                now = dt.datetime.now()
                mod = now.hour * 60 + now.minute
                due = []
                for period in config.PATTERN_PERIODS:
                    iv = _PERIOD_SCAN_MIN.get(period)
                    if iv and mod % iv == 0 and _last_scan_minute.get(period) != mod:
                        _last_scan_minute[period] = mod
                        due.append(period)
                if due:
                    push = _pattern_ready
                    await _pattern_scan_once(push, periods=due)
                    _pattern_ready = True   # 首轮成功后才开始推送（启动静默建基线）
                    await _push_startup_snapshot()
                await asyncio.sleep(30)
            else:
                # 节假日/非交易时段：启动后只拉一遍基线（矩阵有数据可看），之后零拉取
                if not _pattern_ready:
                    await _pattern_scan_once(False)
                    _pattern_ready = True
                    await _push_startup_snapshot()
                    print(f'[pattern_loop] {dt.datetime.now():%H:%M} 非交易时段基线就绪，'
                          f'停止定时拉取，等待开市自动恢复')
            await asyncio.sleep(60)   # 非交易时段每分钟只查一次是否开市（不发任何数据请求）
        except Exception as e:
            print('[pattern_loop] error:', e)
            await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(periodic())
    alert_task = asyncio.create_task(alert_loop())
    pattern_task = asyncio.create_task(pattern_loop())
    yield
    task.cancel()
    alert_task.cancel()
    pattern_task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get('/')
async def index():
    return RedirectResponse('/pc')


@app.get('/pc')
async def page_pc_overview():
    return FileResponse(os.path.join(WEB_DIR, 'pc_overview.html'))


@app.get('/pc/detail')
async def page_pc_detail():
    return FileResponse(os.path.join(WEB_DIR, 'pc_detail.html'))


@app.get('/m')
async def page_m_list():
    return FileResponse(os.path.join(WEB_DIR, 'm_list.html'))


@app.get('/m/detail')
async def page_m_detail():
    return FileResponse(os.path.join(WEB_DIR, 'm_detail.html'))


@app.get('/pc/backtest')
async def page_backtest():
    return FileResponse(os.path.join(WEB_DIR, 'pc_backtest.html'))


@app.get('/pc/help')
async def page_pc_help():
    return FileResponse(os.path.join(WEB_DIR, 'pc_help.html'))


@app.get('/pc/matrix')
async def page_pc_matrix():
    """全级别并行监控矩阵（设计文档 §9，用户主力常驻页面）。"""
    return FileResponse(os.path.join(WEB_DIR, 'pc_matrix.html'))


@app.get('/api/pattern_matrix')
async def api_pattern_matrix():
    """矩阵页全量状态：cells + 事件流 + 行列定义（行列由 config 驱动，改 config 页面零改动）。"""
    return JSONResponse({
        'cells': list(_pattern_cells.values()),
        'events': list(_pattern_events),
        'periods': list(reversed(config.PATTERN_PERIODS)),   # 倒序：日线在最上（加上后）
        'contracts': config.CONTRACTS,
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


@app.get('/api/live')
async def api_live(sym: str, period: str = '15', subpeak: str = '1',
                   algo: str = 'normal', strict: str = '1', fxcheck: str = 'strict',
                   endpeak: str = '1', gap: str = '0',
                   zsextend: str = '1', zsmerge: str = '0', zsfirst: str = '0'):
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
        return JSONResponse(engine.get_live(sym, period, bi_conf=bi_conf, zs_conf=zs_conf))
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
