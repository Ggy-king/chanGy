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

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse

import engine, fees, backtest, trading_time, config

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
async def api_live(sym: str, period: str = '15'):
    """看盘模式：指定周期K线+笔（1/3/15/60/daily），独立于15分钟预警缓存。"""
    if period not in ('1', '3', '15', '60', 'daily'):
        period = '15'
    try:
        return JSONResponse(engine.get_live(sym, period))
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
    uvicorn.run(app, host='0.0.0.0', port=PORT, log_level='warning')
