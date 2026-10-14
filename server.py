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


# ==================== 日志落盘（用户定稿 2026-10-11） ====================
# print 原样输出到终端，同时抄写进 data/logs/server_YYYYMMDD.log。
# 跨天自动换新文件并清理 30 天前的旧文件——在写入时按日期判断，不用定时器，平时零开销。
class _TeeLog:
    KEEP_DAYS = 30

    def __init__(self, stream):
        self._stream = stream
        self._dir = os.path.join(_HERE, 'data', 'logs')
        self._date = None
        self._fh = None

    def _ensure(self):
        d = dt.date.today()
        if d != self._date:
            if self._fh:
                try:
                    self._fh.close()
                except Exception:
                    pass
            os.makedirs(self._dir, exist_ok=True)
            self._fh = open(os.path.join(self._dir, f'server_{d:%Y%m%d}.log'), 'a', encoding='utf-8')
            self._date = d
            self._prune()

    def _prune(self):
        try:
            cutoff = time.time() - self.KEEP_DAYS * 86400
            for fn in os.listdir(self._dir):
                if fn.startswith('server_') and fn.endswith('.log'):
                    p = os.path.join(self._dir, fn)
                    if os.path.getmtime(p) < cutoff:
                        os.remove(p)
        except Exception:
            pass

    def __getattr__(self, name):
        # 未显式实现的属性（isatty/encoding/fileno 等）透传给原终端，
        # 否则 uvicorn 等库访问 sys.stdout.isatty() 会崩（2026-10-11 启动报错）
        return getattr(self._stream, name)

    def isatty(self):
        return False

    def write(self, s):
        try:
            self._ensure()
            if self._fh:
                self._fh.write(s)
                self._fh.flush()
        except Exception:
            pass
        try:
            return self._stream.write(s)
        except Exception:
            return 0

    def flush(self):
        for f in (self._fh, self._stream):
            try:
                if f:
                    f.flush()
            except Exception:
                pass


sys.stdout = _TeeLog(sys.stdout)
sys.stderr = _TeeLog(sys.stderr)
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
    if not _MON['enabled']:
        print('[监控] 总开关为关：跳过启动基线拉取（预警/总览停用，看盘不受影响）')
    else:
        try:
            ov, _ = await engine.refresh_and_detect_all_async()
            await manager.broadcast({'type': 'overview', 'overview': ov, 'ts': time.time()})
        except Exception as e:
            print('initial refresh error:', e)
    while True:
        if not _MON['enabled']:
            await asyncio.sleep(3)   # 总开关关闭：空转零网络请求
            continue
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
            # 价格预警不归监控总开关管（用户定稿 2026-10-12）：看盘配套，永远跑
            if not trading_time.is_trading_time():
                await asyncio.sleep(10)
                continue
            loop = asyncio.get_event_loop()
            # 只拉设了预警线的品种：没设线就零请求（用户定稿 2026-10-11）。
            # 价格改走新浪hq（一次 HTTP 批量请求，线程池里跑），天勤长连接只服务
            # K 线监控，预警不再上去抢锁——新浪实时价与列表页同源（用户定稿 2026-10-11）
            syms = sorted({a['symbol'] for a in alert_manager.get_active_alerts()})
            if not syms:
                await asyncio.sleep(30)
                continue
            raw = await loop.run_in_executor(None, engine.get_quotes, syms)
            quotes = (raw or {}).get('quotes') or {}
            prices = {s: v['price'] for s, v in quotes.items()
                      if isinstance(v, dict) and v.get('price')}
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
_pattern_events = deque(maxlen=100)  # 事件流全部历史（客户端按页签裁：全部50/确认40）
_pattern_ready = False   # 首轮静默：只建基线不推送（§5.2）
_snapshot_done = False   # 启动机会快照只推一次
_snapshot_msgs = []      # 静默基线扫描期间收集的新鲜事件（快照用）

# 笔六开关：监控跟随详情页⚙面板（localStorage cl_bi_conf），进矩阵页时前端同步过来。
# 变更后静默重扫全量（~18s/85单元）并重推机会快照；调试期频繁改动也只在实际进页面时才触发。
# 持久化到 data/pattern_bi_conf.json：重启后立即用上次的笔配置（否则监控用默认配置算笔，
# 与用户详情页图上黄线（自定义配置，笔数差一倍）对不上——成笔判定全错位（2026-10-13）。
_PATTERN_BI_CONF_FILE = os.path.join(_HERE, 'data', 'pattern_bi_conf.json')
_pattern_bi_conf = dict(engine.BI_CONF_DEFAULT)
try:
    with open(_PATTERN_BI_CONF_FILE, encoding='utf-8') as _f:
        _pattern_bi_conf.update(json.load(_f))
except Exception:
    pass


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


# ==================== 指纹去重集合（防无限增长，用户定稿 2026-10-11） ====================
# 指纹格式 pattern|p1|p2 不含时间，无法从指纹本身判断新旧 → 加一层 _DatedSet
# 记录每个指纹的入库日期，每天（写入时按日期判断，不用定时器）清理 30 天前的旧指纹。
# 近期事件流里仍存在的指纹不清理（防止"延续触发"被误清后重复推卡）。
class _DatedSet(set):
    def __init__(self, iterable=()):
        super().__init__(iterable)
        self._dates = {}   # fingerprint -> 'YYYY-MM-DD' 入库日期

    def add(self, fp):
        super().add(fp)
        self._dates[fp] = dt.date.today().isoformat()


_fp_prune_date = None


def _prune_reported(keep_days=30):
    global _fp_prune_date
    today = dt.date.today().isoformat()
    if _fp_prune_date == today:
        return
    _fp_prune_date = today
    cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).isoformat()
    recent = {ev.get('fingerprint') for ev in _pattern_events}
    n = 0
    for rep in _pattern_reported.values():
        if not isinstance(rep, _DatedSet):
            continue
        for fp in [f for f, d in rep._dates.items() if d < cutoff and f not in recent]:
            rep._dates.pop(fp, None)
            rep.discard(fp)
            n += 1
    if n:
        print(f'[pattern] 指纹清理：移除 {keep_days} 天前的旧指纹 {n} 条')


# ==================== 事件快照落盘（用户定稿 2026-10-11） ====================
# 增删改查全在内存做（快），JSON 只是定期把内存抄一份到磁盘：重启后恢复事件流
# （24h 卡片不丢、首次发现时间不重置）和去重指纹（重启后旧结构不会被当新事件重推）。
# 文件永远只有一份、事件最多 100 条（deque 上限），不会越存越多，无需按时间清理。
_PATTERN_SNAPSHOT_FILE = os.path.join(_HERE, 'data', 'pattern_snapshot.json')


def _pattern_save_snapshot():
    try:
        os.makedirs(os.path.dirname(_PATTERN_SNAPSHOT_FILE), exist_ok=True)
        data = {
            'saved_at': dt.datetime.now().isoformat(timespec='seconds'),
            'days_back': _pattern_days_back,
            'events': list(_pattern_events),
            'reported': {f'{s}|{p}': sorted(rep) for (s, p), rep in _pattern_reported.items()},
            'reported_dates': {f'{s}|{p}': dict(getattr(rep, '_dates', {}))
                               for (s, p), rep in _pattern_reported.items()},
        }
        tmp = _PATTERN_SNAPSHOT_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _PATTERN_SNAPSHOT_FILE)
    except Exception as e:
        print('[pattern] 事件快照保存失败:', e)


def _pattern_load_snapshot():
    global _pattern_days_back
    try:
        if not os.path.exists(_PATTERN_SNAPSHOT_FILE):
            return
        with open(_PATTERN_SNAPSHOT_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('days_back') in (2, 3):
            _pattern_days_back = int(data['days_back'])   # 重启后窗口设置不丢
        _ok_syms = {c['symbol'] for c in config.CONTRACTS}
        for ev in data.get('events', []):
            sym = str(ev.get('symbol') or '')
            if sym and sym not in _ok_syms:
                continue   # 合约换月后旧合约历史事件不复活（2026-10-14 生猪2611→2701 残留事故）
            fp = str(ev.get('fingerprint') or '')
            if sym and fp and not fp.startswith(sym + '|'):
                ev['fingerprint'] = f'{sym}|{ev.get("period", "")}|{fp}'   # 旧快照指纹迁移（2026-10-12 加前缀）
            _pattern_events.append(ev)
        for k, fps in (data.get('reported') or {}).items():
            s, _, p = k.rpartition('|')
            if s and s not in _ok_syms:
                continue   # 旧合约去重指纹同步丢弃
            rep = _DatedSet(fps)
            rep._dates = dict((data.get('reported_dates') or {}).get(k, {}))
            _pattern_reported[(s, p)] = rep
        if _pattern_events:
            print(f'[pattern] 已恢复事件快照：{len(_pattern_events)} 条事件'
                  f'（{data.get("saved_at")} 保存），去重指纹已同步恢复')
    except Exception as e:
        print('[pattern] 事件快照恢复失败（按冷启动处理）:', e)


_pattern_load_snapshot()


def _push_rescan_task():
    async def _rescan():
        global _pattern_ready, _snapshot_done
        _pattern_ready = False          # 静默重建基线（新配置下历史指纹全部作废）
        _snapshot_done = False
        await _pattern_scan_once(False)
        _pattern_ready = True
        await _push_startup_snapshot()  # 重推当前机会快照
        _pattern_save_snapshot()        # 重建后事件/指纹可能变化，同步落盘
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
            rep = _pattern_reported.setdefault(key, _DatedSet())
            try:
                # 线程池里跑（fetch+算笔是同步阻塞），避免卡住事件循环
                snap = await asyncio.get_event_loop().run_in_executor(
                    None, engine.pattern_snapshot, sym, period, rep, _pattern_bi_conf)
            except Exception:
                continue   # 掐线重连窗口/空数据 → 跳过该单元本轮，下轮自然恢复
            try:
                await _apply_pattern(sym, name, period, snap['result'], push,
                                     snap.get('bi_tail'))
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


_PATTERN_SCAN_DAYS = 3    # 扫描保留：固定最近 3 个交易日（最大回看档），展示时按用户选择再筛
_pattern_days_back = 3    # 用户选的展示窗口（2/3 个交易日，不含今天）；矩阵页「回看」按钮控制


def _window_start(days_back=None):
    """事件保留窗口起点：今天不算，往前推 days_back 个交易日，从那天 0 点起。
    交易日直接查 trading_time 日历（akshare+本地缓存，跨年自动重拉），
    不做任何夜盘归属/交易时长换算——日历一刀切，零歧义。"""
    import trading_time as tt
    if days_back is None:
        days_back = _PATTERN_SCAN_DAYS
    d = dt.date.today()
    found = 0
    while found < days_back and (dt.date.today() - d).days < 45:
        d -= dt.timedelta(days=1)
        if tt.is_trading_day(d):
            found += 1
    return dt.datetime.combine(d, dt.time(0, 0))


def _outside_show_window(e):
    """展示窗口过滤（仅矩阵接口返回时用）：早于用户选的回看起点
    （2/3 个交易日 0 点）的事件不返回。扫描底仓固定 3 天，与此互不影响。"""
    try:
        t = dt.datetime.fromisoformat(str(e.get('trigger_time') or e.get('time') or ''))
        if t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        return t < _window_start(_pattern_days_back)
    except Exception:
        return False


def _is_stale_structure(time_str):
    """【仅用于推送区/事件流】触发时刻早于扫描保留窗口（固定最近 3 个交易日，
    不含今天，0 点起卡）的推送视为历史——固定 3 天做最大底仓，展示窗口
    （2/3 天）在矩阵接口返回时再筛，切换无需重扫。
    格子（矩阵）不用此函数——格子上永远实时显示状态机的当前阶段，无任何时间过滤。
    解析失败按新鲜处理。"""
    try:
        t = dt.datetime.fromisoformat(str(time_str))
        if t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        return t < _window_start(_PATTERN_SCAN_DAYS)
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


# ---- 成笔判定（2026-10-13 用户定稿）：确认/触发卡的 未成笔/已成笔 徽标，
# 按"图上最新笔"判（engine.pattern_snapshot 的 bi_tail = 完成笔+进行中笔，
# 与详情页黄线画法一致）——黄线画出来就算成笔，不等锁死确认；延伸/被吃跟着最新状态走。


def _bi_formed_in_tail(bi_tail, side, t3, p2):
    """图上成笔判定（用户定稿 2026-10-13：按最新的笔算，图上能画出来就是成立）。
    回调笔特征 = 起点时刻不早于结构锚 t3 且 起点价越过 p2（A类的回调笔起点=
    突破笔端点必破 p2；B类回调笔起点=7 点本身，p2=6 恒成立）。
    t3 之后、突破之前可能有同方向的震荡笔（起点价不越 p2）→ 用起点价排除。
    时间匹配与 merged 坐标无关 → 重启后历史卡照样能翻转。"""
    a3 = str(t3 or '')
    if not a3 or p2 is None:
        return False
    try:
        p2f = float(p2)
    except (TypeError, ValueError):
        return False
    if side == 'long':    # 回调向下笔，起点=突破高点 > p2
        return any(b.get('d') == 'down' and str(b.get('st') or '') >= a3
                   and float(b.get('sp')) > p2f for b in (bi_tail or []))
    return any(b.get('d') == 'up' and str(b.get('st') or '') >= a3
               and float(b.get('sp')) < p2f for b in (bi_tail or []))


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
        if old.get('time') == msg.get('time') and old.get('price') == msg.get('price') \
                and bool(old.get('bi_formed')) == bool(msg.get('bi_formed')):
            return                     # 端点没变且成笔状态没变，无需更新
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


async def _apply_pattern(sym, name, period, result, push, bi_tail=None):
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
            _conf = 'CONF' in ev['pattern'] or 'TRIG' in ev['pattern']   # 确认/触发卡：带成笔状态
            _formed = _conf and _bi_formed_in_tail(bi_tail, side, ev.get('t3'), ev.get('p2'))
            msg = {'type': 'pattern_alert', 'pattern': ev['pattern'],
                   'kind': 'A' if ev['pattern'].startswith('VREV') else 'B',
                   'side': 'long' if 'LONG' in ev['pattern'] or 'BUY' in ev['pattern'] else 'short',
                   'symbol': sym, 'name': name, 'period': period,
                   'price': ev['price'], 'time': ev['time'], 'trigger_time': _trig,
                   'time_str': _fmt_time_str(ev['time']),
                   'p1': ev['p1'], 'p2': ev['p2'], 'p3': ev['p3'],
                   't1': ev.get('t1'), 't2': ev.get('t2'), 't3': ev.get('t3'),
                   'bi_span': ev.get('bi_span'), 'ratio': ev.get('ratio'),
                   'bi_formed': bool(_formed),
                   'brief': _pattern_brief(ev['pattern'], ev),
                   # 指纹带 symbol|period（2026-10-12）：纯 pattern|p1|p2 会让不同周期/品种
                   # 的同锚结构撞车（实测甲醇3分与15分同指纹互相覆盖事件卡，卡片"消失"）
                   'fingerprint': f'{sym}|{period}|{ev["fingerprint"]}',
                   'text': _pattern_text(name, period, ev), 'ts': time.time(),
                   'first_time': ev['time']}   # 快照重建时首检时刻≈触发时刻（内存流不丢首次显示）
            if push:
                await _upsert_event_card(msg)
            else:
                # 基线期（push=False）：重发事件若已有卡片 → 静默原位更新（不广播不响铃）。
                # 否则会被 _push_startup_snapshot 按"指纹已存在"丢弃——重启后成笔翻转
                # 与延续时间全部丢失，卡片永久卡在旧状态（2026-10-13 纸浆/VC 卡死事故）。
                dup = next((m for m in _pattern_events
                            if m.get('fingerprint') == msg['fingerprint']), None)
                if dup is not None:
                    if (dup.get('bi_formed') != msg.get('bi_formed')
                            or dup.get('time') != msg.get('time')
                            or dup.get('price') != msg.get('price')):
                        msg['first_time'] = dup.get('first_time') or dup.get('time')
                        for _i, _m in enumerate(_pattern_events):
                            if _m is dup:
                                _pattern_events[_i] = msg
                                break
                        manager.remember_replace(msg)
                else:
                    _snapshot_msgs.append(msg)   # 全新事件：收集后由快照统一推送

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
                     't1': s.get('t1'), 't2': s.get('t2'), 't3': s.get('t3'),   # 结构三点时刻（详情页画结构区间用）
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
    """静默基线扫描收集到的新鲜事件（候选+确认，均在回看窗口内）按时间升序统一推送。
    不做候选/确认取舍——事件流全量入 deque(100)，矩阵页按页签裁剪（全部50/确认40）。"""
    global _snapshot_done, _snapshot_msgs
    if _snapshot_done:
        return
    _snapshot_done = True
    # 按触发时刻升序（前端再按触发时刻排序展示，新事件置顶）
    _snapshot_msgs.sort(key=lambda m: str(m.get('trigger_time') or m.get('time') or ''))
    seen = {}
    for msg in _snapshot_msgs:
        # 同指纹去重（指纹已带 symbol|period → 只挡同一单元的重复推送，不同级别各留各卡）
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

# ============ 监控总开关（用户定稿 2026-10-12，语义 10-12 修正）============
# 只管"形态监控"：pattern_loop（矩阵卡片）+ periodic（总览预警信号）。
# 价格预警(alert_loop)与手续费刷新(fees)是看盘配套，不归它管、永远运行。
# 开机保持关闭则形态监控不启动；运行时可在监控页设置切换，看盘不受影响。
_MON_FILE = os.path.join(_HERE, 'data', 'monitor_switch.json')
def _monitor_enabled():
    try:
        with open(_MON_FILE, encoding='utf-8') as f:
            return bool(json.load(f).get('enabled', True))
    except Exception:
        return True
_MON = {'enabled': _monitor_enabled()}
def _monitor_save():
    try:
        with open(_MON_FILE, 'w', encoding='utf-8') as f:
            json.dump({'enabled': _MON['enabled']}, f)
    except Exception as e:
        print('[监控开关] 保存失败:', e)


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
            if not _MON['enabled']:
                await asyncio.sleep(3)   # 总开关关闭：不扫描不拉行情，看盘不受影响
                continue
            now = dt.datetime.now()
            gap = _last_loop_ts is not None and (now - _last_loop_ts).total_seconds() > 240
            _prune_reported()   # 每天首次进入循环时清一次 30 天前的旧指纹（日期判断，非定时器）
            if trading_time.is_trading_time():
                if not _pattern_ready:
                    # 启动基线：全级别立即扫一遍（否则 60 分级别要等下一个整点，矩阵空窗太长），
                    # 之后进入挂钟对齐节奏；基线静默不推送
                    await _pattern_scan_once(False)
                    _pattern_ready = True
                    _baseline_ts = time.time()
                    await _push_startup_snapshot()
                    _pattern_save_snapshot()
                    print(f'[pattern_loop] {dt.datetime.now():%H:%M} 启动基线扫描完成，进入挂钟对齐节奏')
                elif gap:
                    print(f'[pattern_loop] {now:%H:%M} 停摆恢复（间隔超 4 分钟），立即补扫全级别')
                    await _pattern_scan_once(True)
                    _pattern_save_snapshot()
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
                            _pattern_save_snapshot()
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
                        _pattern_save_snapshot()
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
                    _pattern_save_snapshot()
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
    # 手续费不归监控总开关管（用户定稿 2026-10-12）：看盘配套，永远跑
    try:
        await asyncio.get_event_loop().run_in_executor(None, lambda: fees.load(force=True))
        print('[fees] 启动刷新完成（手续费/保证金）')
    except Exception as e:
        print('[fees] 启动刷新失败（沿用旧缓存）：', e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动自检：关键配置缺失时明确说出来，避免"静默失败跑几天才发现"（2026-10-11）
    if not os.path.exists(os.path.join(_HERE, 'secret.py')):
        print('[启动自检] !! 缺少 secret.py（天勤账号配置），行情登录会失败')
    if not os.path.exists(os.path.join(_HERE, 'data', 'gitee_conf.json')):
        print('[启动自检] !! 缺少 data/gitee_conf.json（持仓云同步配置），持仓推送会一直失败')
    else:
        threading.Thread(target=_sync_contracts_to_gitee, daemon=True).start()   # 后台执行，启动不等网络
    if not _MON['enabled']:
        print('[监控] 总开关为关：本次启动不扫描/不预警/不拉行情（看盘不受影响）；可在监控页设置里开启')
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


@app.get('/pc/positions')
async def page_pc_positions():
    """持仓驾驶舱（2026-10-14）：持有许可灯+实时判决，独立页面常开大字显示。"""
    return _page('pc_positions.html')


@app.get('/pc/stats')
async def page_pc_stats():
    """交易统计监督页（2026-10-14）：纪律指标为主、结果指标为辅，自动生成账本结论。"""
    return _page('pc_stats.html')


@app.get('/struct_zone.js')
async def asset_struct_zone_js():
    """结构区间浮层模块（详情页引用；独立于中枢，2026-10-12 用户定稿）"""
    return _page('struct_zone.js')


@app.get('/popup_cards.js')
async def asset_popup_cards_js():
    """右上角预警弹卡全局组件（监控/详情/列表页共用，2026-10-12 用户定稿）"""
    return _page('popup_cards.js')


@app.get('/favicon.svg')
async def asset_favicon_svg():
    return _page('favicon.svg')


@app.get('/favicon.png')
async def asset_favicon_png():
    return _page('favicon.png')


@app.get('/positions_core.js')
async def asset_positions_core_js():
    """移动端持仓共享逻辑层（/m 与 APK 离线壳共用，PC 端不动，2026-10-12 用户定稿）"""
    return _page('positions_core.js')


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
        'events': [e for e in _pattern_events if not _outside_show_window(e)],
        'periods': list(reversed(config.PATTERN_PERIODS)),   # 倒序：日线在最上（加上后）
        'contracts': config.CONTRACTS,
        'ready': _snapshot_done,
        'days_back': _pattern_days_back,
        'mon_enabled': _MON['enabled'],
        'ts': time.time(),
    })


@app.post('/api/pattern_days')
async def api_pattern_days(request: Request):
    """矩阵页「回看」窗口设置（2/3 个交易日，不含今天、0 点起卡）。
    即时生效：服务端扫描始终保留 3 个交易日底仓，此设置只控制接口返回时筛哪个窗口，
    切换不重扫——页面改完重拉一次矩阵即可（毫秒级）。"""
    global _pattern_days_back
    try:
        raw = await request.json()
    except Exception:
        raw = {}
    days = raw.get('days') if isinstance(raw, dict) else None
    if days not in (2, 3):
        return JSONResponse({'ok': False, 'error': 'days 只能是 2 或 3'})
    days = int(days)
    changed = days != _pattern_days_back
    if changed:
        _pattern_days_back = days
        _pattern_save_snapshot()
    return JSONResponse({'ok': True, 'changed': changed, 'days': days})


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
    try:   # 持久化：重启后监控直接用上次的笔配置，与详情页图上黄线保持一致
        with open(_PATTERN_BI_CONF_FILE, 'w', encoding='utf-8') as f:
            json.dump(new_conf, f, ensure_ascii=False)
    except Exception:
        pass
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
_CONTRACTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'contracts.json')
_REMOTE_LOCAL = {'positions.json': _POSITIONS_FILE, 'history.json': _HISTORY_FILE, 'contracts.json': _CONTRACTS_FILE}
_POS_EXIT_MAP = {'二买': '二卖', '二卖': '二买', '三买': '三卖', '三卖': '三买'}
_POS_WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
_POS_LIMIT = 4
_POS_LOCK = threading.Lock()
_GITEE_SHA = {}                                          # remote_path -> 上次已同步 sha
_POS_DIRTY = {'positions.json': False, 'history.json': False, 'contracts.json': False}
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


def _sync_contracts_to_gitee():
    """把 config.CONTRACTS 落成 data/contracts.json 并推 Gitee（2026-10-12）。
    手机端 positions.html 启动时拉它 → 改 config 重启后手机合约自动跟随，无需重打 APK。
    内容没变就不标脏（避免每次重启都多一个 Gitee 提交）。"""
    try:
        data = {'contracts': [{'symbol': c['symbol'], 'name': c['name']} for c in config.CONTRACTS],
                'ts': time.time()}
        new_text = json.dumps(data, ensure_ascii=False, indent=1)
        try:
            with open(_CONTRACTS_FILE, encoding='utf-8') as f:
                old_contracts = json.load(f).get('contracts', [])
        except Exception:
            old_contracts = None
        if old_contracts != data['contracts']:
            with open(_CONTRACTS_FILE, 'w', encoding='utf-8') as f:
                f.write(new_text)
            need_push = True
        else:
            # 本地与 config 一致，但远端可能缺失（如上次推送失败后重启，脏标记已丢）→ 查一次远端补推
            need_push = False
            conf = _gitee_conf()
            if conf.get('token'):
                try:
                    api, _c = _gitee_api('contracts.json')
                    r = requests.get(api, params={'access_token': conf['token'],
                                                  'ref': conf.get('branch', 'master')}, timeout=8)
                    if not (r.status_code == 200 and isinstance(r.json(), dict)):
                        need_push = True
                except Exception:
                    pass   # 查远端失败不阻塞启动，等下次重启或改 config 时再补
        if need_push:
            _POS_DIRTY['contracts.json'] = True
            _bg_sync()
            print('[合约] contracts.json 已更新并推送 Gitee')
    except Exception as e:
        print('[合约] contracts.json 同步失败（不影响运行）:', str(e)[:120])


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
        # Gitee 规则：新建文件必须 POST（PUT 会报 "sha is missing"），更新才用 PUT
        r = (requests.put if sha else requests.post)(api, json=body, timeout=15)
        if r.status_code in (200, 201):
            _GITEE_SHA[remote] = (r.json().get('content') or {}).get('sha') or sha
            return ''
        if sha:
            _GITEE_SHA[remote] = None   # sha 冲突等：作废缓存，下轮重取再推
        return f'{"PUT" if sha else "POST"} {r.status_code}: {r.text[:150]}'
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
                    err = _gitee_push_file(remote, _REMOTE_LOCAL[remote])
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


def _history_pull_merge(force=False):
    """拉取远端 history.json 并按 id 合并进本地（2026-10-13）。
    根因修复：此前 PC 从不回拉 history.json，本地缓存陈旧；PC 端平仓时会把
    陈旧本地 history 整体推上 Gitee，覆盖掉手机端写入的记录（保本简报丢失）。"""
    conf = _gitee_conf()
    if not (conf.get('token') and conf.get('owner')):
        return
    try:
        api, conf = _gitee_api('history.json')
        r = requests.get(api, params={'access_token': conf['token'],
                                      'ref': conf.get('branch', 'master')}, timeout=8)
        if r.status_code != 200:
            return
        d = r.json()
        if not isinstance(d, dict) or not d.get('sha'):
            return
        if not force and d['sha'] == _GITEE_SHA.get('history.json'):
            return
        remote = json.loads(base64.b64decode(d['content']).decode()).get('history', [])
        with _POS_LOCK:
            if _POS_DIRTY.get('history.json'):
                # 本地有未推送变更 → 并集合并（保护本地新记录不被远端覆盖）。
                # 注意并集无法传播别处做的"删除"：云端删掉的记录若本地还有（非脏），
                # 走下面的远端权威分支才会跟着删。
                local = _history_load()
                rids = {x.get('id') for x in remote}
                merged = [x for x in local if x.get('id') not in rids] + remote
                _history_save(merged)
            else:
                # 本地无未推送变更 → 远端权威，整体采纳（云端删除/修改都能同步到本地；
                # 2026-10-14 鸡蛋记录事故：云端删了本地并集保留，统计里永远阴魂不散）
                _history_save(remote)
        _GITEE_SHA['history.json'] = d['sha']
    except Exception:
        pass


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
    _history_pull_merge()   # history.json 同步回灌（手机端平仓简报→电脑可见)


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
        if p.get('entry_fp'):   # 进场时的活跃结构指纹（持仓卡"回调笔延伸中"判定用，2026-10-13）
            rec['entry_fp'] = str(p['entry_fp'])[:120]
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
        for k in ('entry', 'exit', 'note', 'result', 'pnl', 'period', 'price'):
            if k in p:
                rec[k] = str(p[k])[:200]
        if 'entry' in p:
            rec['exit'] = str(p.get('exit') or _POS_EXIT_MAP.get(rec['entry'], rec['entry']))
        _positions_save(positions)
    _POS_DIRTY['positions.json'] = True
    _bg_sync()
    return JSONResponse({'ok': True, 'positions': positions})


@app.post('/api/monitor_switch')
async def api_monitor_switch(request: Request):
    """监控总开关（用户定稿 2026-10-12）：关=四个循环空转零网络请求，只剩看盘；持久化到 data/。"""
    p = await request.json()
    _MON['enabled'] = bool(p.get('enabled'))
    _monitor_save()
    state_txt = '开' if _MON['enabled'] else '关'
    print('[监控] 总开关 → ' + state_txt + ('' if _MON['enabled'] else '（扫描/预警/手续费刷新全部停用，看盘不受影响）'))
    try:
        await manager.broadcast({'type': 'monitor_state', 'enabled': _MON['enabled']})
    except Exception:
        pass
    return JSONResponse({'ok': True, 'enabled': _MON['enabled']})


@app.post('/api/positions/delete')
async def api_positions_delete(request: Request):
    """平仓删除：记录移入 history.json（简报：止盈/止损/保本 + 盈亏，选填；
    自动补持仓时长/平仓时间/星期），positions 与 history 一起推 Gitee。"""
    p = await request.json()
    pid = str(p.get('id', ''))
    result = str(p.get('close_result', '') or '')
    pnl = str(p.get('close_pnl', '') or '')
    _history_pull_merge(force=True)   # 写前强拉远端合并，防止陈旧本地 history 推送覆盖手机记录
    with _POS_LOCK:
        positions = _positions_load()
        rec = next((x for x in positions if x['id'] == pid), None)
        if rec is None:
            return JSONResponse({'error': '未找到该持仓'}, status_code=404)
        now = dt.datetime.now()
        try:
            opened = dt.datetime.fromisoformat(str(rec.get('open_iso') or '') or now.isoformat())
            if opened.tzinfo is not None:
                # 手机端 open_iso 是 UTC 带时区格式（new Date().toISOString()），转本地 naive，
                # 否则 now-opened 报 "naive and offset-aware" TypeError → 500 → 前端静默失败
                opened = opened.astimezone().replace(tzinfo=None)
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
    _history_pull_merge()   # 手机端简报先回灌，统计页看到的就是全量最新
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
    # ============ 启动命令行（用户定稿 2026-10-12）：↑↓ 选择 + 回车确认；-y 全默认 ============
    import sys as _sys

    _ASK = _sys.stdin.isatty() and not ('-y' in _sys.argv[1:] or '--yes' in _sys.argv[1:])

    _C, _Y, _G, _R, _B, _D, _RST = '\033[36m', '\033[33m', '\033[32m', '\033[31m', '\033[1m', '\033[2m', '\033[0m'

    def _mask(u):
        u = str(u)
        return u[:3] + '****' + u[-2:] if len(u) > 7 else u[0] + '****'

    def _load_accounts():
        """secret.py 只存数据；本函数负责读取与兼容（新 ACCOUNTS / 旧 TQ_USER）"""
        try:
            import secret
            accs = getattr(secret, 'ACCOUNTS', None)
            if accs:
                return [(a.get('name') or a['user'], a['user'], a['pwd']) for a in accs]
            return [('main', secret.TQ_USER, secret.TQ_PASS)]
        except ImportError:
            return []

    def _pick(title, options, default_idx):
        """↑↓ 移动 + 回车确认（零依赖）；Windows 用 msvcrt，Unix 用 termios；
非交互环境（管道/自启）直接用默认"""
        if not _ASK:
            return default_idx
        sel = max(0, min(default_idx, len(options) - 1))

        def _draw(first):
            if not first:
                print(f'\x1b[{len(options)}A', end='')   # 光标上移重画
            for i, o in enumerate(options):
                cur, hl = ('❯ ', _C + _B) if i == sel else ('  ', _D)
                print(f'  {cur}{hl}{o}{_RST}\x1b[K')

        print(title)
        if _sys.platform == 'win32':
            import msvcrt
            # 旧版 conhost 需手动开启 ANSI 转义序列；Windows Terminal 默认已开
            try:
                import ctypes
                _k32 = ctypes.windll.kernel32
                _h = _k32.GetStdHandle(-11)
                _mode = ctypes.c_uint32()
                if _k32.GetConsoleMode(_h, ctypes.byref(_mode)):
                    _k32.SetConsoleMode(_h, _mode.value | 0x0004)
            except Exception:
                pass
            print('\x1b[?25l', end='')   # 隐藏光标
            _draw(True)
            try:
                while True:
                    ch = msvcrt.getwch()
                    if ch in ('\x00', '\xe0'):    # 功能键前缀，需再读一键
                        k = msvcrt.getwch()
                        if k == 'H':              # ↑
                            sel = (sel - 1) % len(options)
                        elif k == 'P':            # ↓
                            sel = (sel + 1) % len(options)
                    elif ch in ('\r', '\n'):
                        return sel
                    elif ch == '\x03':
                        raise KeyboardInterrupt
                    _draw(False)
            finally:
                print('\x1b[?25h', end='')
            return sel
        import termios, tty
        fd = _sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        print('\x1b[?25l', end='')   # 隐藏光标
        try:
            tty.setcbreak(fd)
            _draw(True)
            while True:
                ch = _sys.stdin.read(1)
                if ch == '\x1b':
                    seq = _sys.stdin.read(2)
                    if seq == '[A':
                        sel = (sel - 1) % len(options)
                    elif seq == '[B':
                        sel = (sel + 1) % len(options)
                elif ch in ('\r', '\n'):
                    return sel
                elif ch == '\x03':
                    raise KeyboardInterrupt
                _draw(False)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
            print('\x1b[?25h', end='')

    print()
    print(f'  {_B}⚡ 缠论预警 · 监控服务{_RST}')
    print(f'  {_D}{"─" * 48}{_RST}')

    # ---- ① 天勤账户（先选账户，用户定稿 2026-10-12）----
    accs = _load_accounts()
    acc_name = ''
    if not accs:
        print(f'  {_C}①{_RST} {_B}天勤账户{_RST}   {_R}⚠ 未找到 secret.py —— 行情登录会失败{_RST}')
    else:
        _LAST_ACC = os.path.join(_HERE, 'data', 'last_account.json')
        last_user = ''
        try:
            with open(_LAST_ACC, encoding='utf-8') as f:
                last_user = json.load(f).get('user', '')
        except Exception:
            pass
        default_i = next((i for i, a in enumerate(accs) if a[1] == last_user), 0)
        opts = [f'👤 {name}   {_mask(user)}' for name, user, _p in accs]
        idx = _pick(f'  {_C}①{_RST} {_B}天勤账户{_RST} {_D}(↑↓ 移动，回车确认){_RST}', opts, default_i)
        name, user, pwd = accs[idx]
        acc_name = name
        import tqsdk_data as _tq
        _tq.set_account(user, pwd)
        try:
            with open(_LAST_ACC, 'w', encoding='utf-8') as f:
                json.dump({'name': name, 'user': user}, f)
        except Exception:
            pass
        print(f'  {_C}①{_RST} 账户 → {_G}{name}{_RST} {_D}({_mask(user)}){_RST}')

    # ---- ② 监控服务 ----
    mon_opts = ['🔔 开启   （扫描 · 矩阵卡片 · 总览信号）',
                '🌙 关闭   （只剩看盘；价格预警/手续费不受影响）']
    idx = _pick(f'  {_C}②{_RST} {_B}监控服务{_RST} {_D}(↑↓ 移动，回车确认){_RST}', mon_opts, 0 if _MON['enabled'] else 1)
    _MON['enabled'] = (idx == 0)
    _monitor_save()
    print(f'  {_C}②{_RST} 监控 → ' + (_G + '🔔 开启' if _MON['enabled'] else _D + '🌙 关闭（价格预警/手续费/看盘不受影响）') + _RST)

    print(f'  {_D}{"─" * 48}{_RST}')
    ip = lan_ip()
    mon_txt = (_Y + '🔔 开' if _MON['enabled'] else _D + '🌙 关') + _RST
    print(f'  🚀 {_B}启动中{_RST} → {_C}http://localhost:{PORT}/pc{_RST}')
    print(f'     📱 手机列表  http://{ip}:{PORT}/m   （手机连同一 WiFi）')
    print(f'     🔔 监控: {mon_txt}   👤 账户: {acc_name or "（未配置）"}   {_D}Ctrl+C 退出{_RST}')
    print(f'  {_D}{"─" * 48}{_RST}')
    # 预初始化天勤连接（后台线程，不阻塞启动）
    def _init_tq():
        try:
            import tqsdk_data as tq
        except Exception as e:
            print(f'[TqData] 导入数据源模块失败: {e}')
            return
        if not accs:
            return
        try:
            tq.TqData()._ensure_api()
            print(f'[TqData] 天勤连接预初始化完成（账户: {acc_name}）')
        except tq.TqAuthError as e:
            # 致命认证错误：明确提示用户改 secret.py 后重启，不会静默重试
            print(f'[TqData] 预初始化失败（致命 - 不会自动重试）: {e}')
        except Exception as e:
            # 网络等瞬时错误：保留旧的"首次请求时会自动重试"提示
            print(f'[TqData] 预初始化失败（首次请求时会自动重试）: {e}')
    import threading as _th
    _th.Thread(target=_init_tq, daemon=True).start()
    uvicorn.run(app, host='0.0.0.0', port=PORT, log_level='warning')
