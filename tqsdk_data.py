# -*- coding: utf-8 -*-
"""
天勤量化 TqSdk 数据源封装
- 合约代码转换：CF2701 -> CZCE.CF701
- 周期用秒数：30秒=30, 3分钟=180, 15分钟=900, 60分钟=3600, 日线=86400
- 单例模式，全局一个TqApi连接，心跳线程保活
"""
import sys, os, types, logging, threading, time, re

# ========== mock 冲突库 ==========
_original_log = logging.Logger._log
def _patched_log(self, level, msg, args, exc_info=None, extra=None, stack_info=False, **kwargs):
    return _original_log(self, level, msg, args, exc_info=exc_info, extra=extra, stack_info=stack_info)
logging.Logger._log = _patched_log

for mod in ['pyarrow','pyarrow.lib','pyarrow.compute','numexpr','numexpr.interpreter',
            'bottleneck','bottleneck.move','scipy','scipy.stats','scipy.sparse',
            'scipy._lib','scipy.special','scipy.linalg','scipy.interpolate',
            'scipy.optimize','scipy.integrate','scipy.fft','scipy.signal',
            'scipy.ndimage','scipy.spatial','scipy.cluster','scipy.io',
            'scipy.misc','scipy.constants','tqsdk_ctpse','tqsdk_sm']:
    if mod not in sys.modules:
        m = types.ModuleType(mod); m.__version__ = '0.0.0'; sys.modules[mod] = m
sys.modules['tqsdk_ctpse'].get_system_info = lambda: {}
class TqCTPSEUnsupportedPlatform(Exception): pass
sys.modules['tqsdk_ctpse'].TqCTPSEUnsupportedPlatform = TqCTPSEUnsupportedPlatform
sys.modules['tqsdk_sm'].get_sm_path = lambda: ''
class _SMContext:
    def __init__(self, *a, **kw): pass
    def __enter__(self): return self
    def __exit__(self, *a): pass
sys.modules['tqsdk_sm'].SMContext = _SMContext
sys.modules['tqsdk_sm'].NullContext = _SMContext

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'pylibs'))
sys.path.insert(0, _HERE)

import pandas as pd
from tqsdk import TqApi, TqAuth

# 品种 -> (交易所, 天勤品种代码) 映射
_PRODUCT_MAP = {
    # 郑商所 CZCE（大写）
    'CF':('CZCE','CF'), 'UR':('CZCE','UR'), 'MA':('CZCE','MA'), 'SA':('CZCE','SA'),
    'SR':('CZCE','SR'), 'CJ':('CZCE','CJ'), 'AP':('CZCE','AP'), 'PK':('CZCE','PK'),
    'FG':('CZCE','FG'), 'TA':('CZCE','TA'), 'OI':('CZCE','OI'), 'RM':('CZCE','RM'),
    'CY':('CZCE','CY'), 'SF':('CZCE','SF'), 'SM':('CZCE','SM'), 'PF':('CZCE','PF'),
    'SH':('CZCE','SH'), 'ZC':('CZCE','ZC'),
    # 上期所 SHFE（小写）
    'rb':('SHFE','rb'), 'RB':('SHFE','rb'), 'cu':('SHFE','cu'), 'CU':('SHFE','cu'),
    'au':('SHFE','au'), 'AU':('SHFE','au'), 'ag':('SHFE','ag'), 'AG':('SHFE','ag'),
    'al':('SHFE','al'), 'AL':('SHFE','al'), 'zn':('SHFE','zn'), 'ZN':('SHFE','zn'),
    'ni':('SHFE','ni'), 'NI':('SHFE','ni'), 'sp':('SHFE','sp'), 'SP':('SHFE','sp'),
    'ru':('SHFE','ru'), 'RU':('SHFE','ru'), 'hc':('SHFE','hc'), 'HC':('SHFE','hc'),
    'fu':('SHFE','fu'), 'FU':('SHFE','fu'), 'bu':('SHFE','bu'), 'BU':('SHFE','bu'),
    'pb':('SHFE','pb'), 'PB':('SHFE','pb'), 'sn':('SHFE','sn'), 'SN':('SHFE','sn'),
    'ss':('SHFE','ss'), 'SS':('SHFE','ss'), 'wr':('SHFE','wr'), 'WR':('SHFE','wr'),
    # 大商所 DCE（小写）
    'm':('DCE','m'), 'M':('DCE','m'), 'y':('DCE','y'), 'Y':('DCE','y'),
    'jm':('DCE','jm'), 'JM':('DCE','jm'), 'jd':('DCE','jd'), 'JD':('DCE','jd'),
    'v':('DCE','v'), 'V':('DCE','v'), 'lh':('DCE','lh'), 'LH':('DCE','lh'),
    'pg':('DCE','pg'), 'PG':('DCE','pg'), 'p':('DCE','p'), 'P':('DCE','p'),
    'c':('DCE','c'), 'C':('DCE','c'), 'cs':('DCE','cs'), 'CS':('DCE','cs'),
    'pp':('DCE','pp'), 'PP':('DCE','pp'), 'l':('DCE','l'), 'L':('DCE','l'),
    'i':('DCE','i'), 'I':('DCE','i'), 'j':('DCE','j'), 'J':('DCE','j'),
    'eb':('DCE','eb'), 'EB':('DCE','eb'), 'eg':('DCE','eg'), 'EG':('DCE','eg'),
    'a':('DCE','a'), 'A':('DCE','a'), 'b':('DCE','b'), 'B':('DCE','b'),
    # 能源中心 INE（小写）
    'sc':('INE','sc'), 'SC':('INE','sc'), 'lu':('INE','lu'), 'LU':('INE','lu'),
    'nr':('INE','nr'), 'NR':('INE','nr'), 'bc':('INE','bc'), 'BC':('INE','bc'),
    # 中金所 CFFEX（大写）
    'IM':('CFFEX','IM'), 'IC':('CFFEX','IC'), 'IF':('CFFEX','IF'), 'IH':('CFFEX','IH'),
    'T':('CFFEX','T'), 'TF':('CFFEX','TF'), 'TS':('CFFEX','TS'), 'TL':('CFFEX','TL'),
    # 广期所 GFEX（大写）
    'SI':('GFEX','SI'), 'LC':('GFEX','LC'), 'PS':('GFEX','PS'),
}

def to_tq_symbol(symbol):
    """CF2701 -> CZCE.CF701, RB2701 -> SHFE.rb2701, jm2701 -> DCE.jm2701
    郑商所用3位月份，其他交易所用4位月份"""
    m = re.match(r'([A-Za-z]+)(\d+)', symbol)
    if not m:
        return symbol
    product = m.group(1)
    contract = m.group(2)
    info = _PRODUCT_MAP.get(product)
    if info:
        exch, tq_product = info
        if exch == 'CZCE':
            contract_short = contract[-3:] if len(contract) > 3 else contract
            return f'{exch}.{tq_product}{contract_short}'
        else:
            return f'{exch}.{tq_product}{contract}'
    if product[0].isupper():
        contract_short = contract[-3:] if len(contract) > 3 else contract
        return f'CZCE.{product}{contract_short}'
    else:
        return f'SHFE.{product}{contract}'


class TqData:
    """单例长连接：全局一个TqApi，心跳线程保活，异常自动重连。"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._api = None
                    inst._api_lock = threading.Lock()
                    inst._klines = {}
                    inst._heartbeat_thread = None
                    inst._heartbeat_stop = False
                    inst._last_heartbeat = 0
                    cls._instance = inst
        return cls._instance

    def _create_api(self):
        """创建新的TqApi连接（调用方必须已持有_api_lock）"""
        # 先关闭旧连接
        if self._api:
            try:
                self._api.close()
            except:
                pass
        self._api = None
        self._klines = {}
        # 创建新连接
        self._api = TqApi(auth=TqAuth('guangyuan', 'Ggy20030111'))
        print('[TqData] 天勤连接成功')
        self._last_heartbeat = time.time()
        # 启动心跳线程
        self._start_heartbeat()

    def _ensure_api(self):
        """确保连接可用，不可用时重连。注意：不要在持有_api_lock时调用！"""
        if self._api is None:
            with self._api_lock:
                if self._api is None:
                    self._create_api()
                    # 凭证放本地 secret.py（已 .gitignore，不入库）；
                    # 首次使用参考模板 secret.py.local
                    try:
                        import secret
                        user, pwd = secret.TQ_USER, secret.TQ_PASS
                    except ImportError:
                        raise RuntimeError(
                            '缺少 secret.py：请复制项目根目录的 secret.py.local 为 secret.py，'
                            '填入天勤账号密码（该文件不入库）')
                    self._api = TqApi(auth=TqAuth(user, pwd))
                    print('[TqData] 天勤连接成功')
        return self._api

    def _start_heartbeat(self):
        """启动心跳线程，每30秒调用一次wait_update保活"""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_stop = False
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        print('[TqData] 心跳保活线程已启动')

    def _heartbeat_loop(self):
        """心跳循环：每30秒调用wait_update，防止服务器主动断开"""
        while not self._heartbeat_stop:
            time.sleep(30)
            if self._heartbeat_stop:
                break
            try:
                if self._api and self._api_lock.acquire(blocking=False):
                    try:
                        if self._api:
                            self._api.wait_update(deadline=5)
                            self._last_heartbeat = time.time()
                    except Exception as e:
                        print(f'[TqData] 心跳异常，准备重连: {e}')
                        self._api = None
                        self._klines = {}
                    finally:
                        self._api_lock.release()
            except:
                pass

    def get_kline(self, symbol, period_sec, data_length=2000):
        """获取K线。已订阅的只等2秒，新订阅的等10秒，异常自动重连。"""
        tq_sym = to_tq_symbol(symbol)
        key = (tq_sym, period_sec)
        max_retries = 3
        for attempt in range(max_retries):
            try:
                api = self._ensure_api()
                with self._api_lock:
                    if key not in self._klines:
                        self._klines[key] = api.get_kline_serial(tq_sym, period_sec, data_length=data_length)
                        api.wait_update(deadline=10)
                    else:
                        api.wait_update(deadline=2)
                    k = self._klines[key]
                    df = k.copy()
                # 转换时间戳
                df['datetime'] = pd.to_datetime(df['datetime'], unit='ns', utc=True).dt.tz_convert('Asia/Shanghai')
                df = df[['datetime','open','high','low','close','volume']].dropna(subset=['open'])
                df = df[df['open'] > 0].reset_index(drop=True)
                return df
            except Exception as e:
                print(f'[TqData] 连接异常(第{attempt+1}次)，自动重连: {e}')
                # 重连：直接在锁内创建新连接，避免死锁
                with self._api_lock:
                    try:
                        self._create_api()
                    except Exception as e2:
                        print(f'[TqData] 重连失败: {e2}')
                if attempt == max_retries - 1:
                    raise
                time.sleep(2)

    def close(self):
        self._heartbeat_stop = True
        if self._api:
            try:
                self._api.close()
            except:
                pass
            self._api = None
            self._klines = {}


# 便捷函数
def fetch_kline(symbol, period_sec, data_length=2000):
    return TqData().get_kline(symbol, period_sec, data_length)


if __name__ == '__main__':
    print('测试天勤数据源...')
    df = fetch_kline('CF2701', 900, 5)
    print(df.to_string())
    print(f'共 {len(df)} 根')
