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
import sys, os, time, asyncio, datetime as dt, socket
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
    """刷新全部品种；有新信号则广播。"""
    if broadcast:
        await manager.broadcast({'type': 'status', 'text': '正在刷新全部品种行情…', 'ts': time.time()})
    ov, new_sigs = await engine.refresh_and_detect_all_async()
    if broadcast:
        await manager.broadcast({'type': 'overview', 'overview': ov, 'ts': time.time()})
        for s in new_sigs:
            msg = {'type': 'signal', 'symbol': s['symbol'], 'name': s['name'],
                   'side': s['side'], 'price': s['price'], 'time': s['time'],
                   'reason': s['reason'] + '（15分定时）', 'simulated': False,
                   'from': 'server', 'ts': time.time()}
            if s.get('risk'):
                msg['risk'] = s['risk']
            manager.remember(msg)
            await manager.broadcast(msg)
        if not new_sigs:
            await manager.broadcast({'type': 'status', 'text': '已刷新，暂无新信号', 'ts': time.time()})
    return ov, new_sigs


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(periodic())
    yield
    task.cancel()


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
                   endpeak: str = '1', gap: str = '0'):
    """看盘模式：指定周期K线+笔，独立于15分钟预警缓存。
    六个笔开关对应 chan.py CBiConfig，默认值=原版默认：
    algo(normal/fx) strict(1/0) fxcheck(loss/half/strict/totally)
    endpeak(1/0) subpeak(1/0) gap(1/0)"""
    if period not in ('30', '1', '3', '15', '60', 'daily'):
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
    try:
        return JSONResponse(engine.get_live(sym, period, bi_conf=bi_conf))
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
