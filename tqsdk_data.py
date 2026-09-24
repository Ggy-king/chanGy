# -*- coding: utf-8 -*-
"""
天勤量化 TqSdk 数据源封装
- 合约代码转换：CF2701 -> CZCE.CF701
- 周期用秒数：30秒=30, 3分钟=180, 15分钟=900, 60分钟=3600, 日线=86400
- 单例模式，全局一个TqApi连接
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
# 郑商所/中金所/广期所品种代码大写；上期所/大商所/能源中心品种代码小写
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
        # 郑商所用3位月份，其他交易所用4位月份
        if exch == 'CZCE':
            contract_short = contract[-3:] if len(contract) > 3 else contract
            return f'{exch}.{tq_product}{contract_short}'
        else:
            return f'{exch}.{tq_product}{contract}'
    # 兜底：大写品种默认郑商所（3位），小写默认上期所（4位）
    if product[0].isupper():
        contract_short = contract[-3:] if len(contract) > 3 else contract
        return f'CZCE.{product}{contract_short}'
    else:
        return f'SHFE.{product}{contract}'


class TqData:
    """单例长连接：全局一个TqApi，订阅过的K线序列缓存，后续只等更新不重建连。"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._api = None
                    inst._api_lock = threading.Lock()
                    inst._klines = {}  # {(tq_sym, period_sec): kline_serial}
                    cls._instance = inst
        return cls._instance

    def _ensure_api(self):
        if self._api is None:
            with self._api_lock:
                if self._api is None:
                    self._api = TqApi(auth=TqAuth('guangyuan', 'Ggy20030111'))
                    print('[TqData] 天勤连接成功')
        return self._api

    def get_kline(self, symbol, period_sec, data_length=2000):
        """获取K线。已订阅的只等2秒，新订阅的等10秒，异常自动重连。"""
        api = self._ensure_api()
        tq_sym = to_tq_symbol(symbol)
        key = (tq_sym, period_sec)
        with self._api_lock:
            try:
                if key not in self._klines:
                    self._klines[key] = api.get_kline_serial(tq_sym, period_sec, data_length=data_length)
                    api.wait_update(deadline=10)  # 新订阅等数据到位
                else:
                    api.wait_update(deadline=2)   # 已订阅只等2秒，避免长时间阻塞
                k = self._klines[key]
                df = k.copy()
            except Exception as e:
                print(f'[TqData] 连接异常，自动重连: {e}')
                self._api = None
                self._klines = {}
                api = self._ensure_api()
                self._klines[key] = api.get_kline_serial(tq_sym, period_sec, data_length=data_length)
                api.wait_update(deadline=10)
                k = self._klines[key]
                df = k.copy()
        # 转换时间戳（纳秒UTC -> 北京时间，保留时区信息）
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ns', utc=True).dt.tz_convert('Asia/Shanghai')
        # 只保留需要的列，去掉空行
        df = df[['datetime','open','high','low','close','volume']].dropna(subset=['open'])
        df = df[df['open'] > 0].reset_index(drop=True)
        return df

    def close(self):
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
