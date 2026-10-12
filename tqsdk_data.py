# -*- coding: utf-8 -*-
"""
天勤量化 TqSdk 数据源封装
- 合约代码转换：CF2701 -> CZCE.CF701
- 周期用秒数：30秒=30, 3分钟=180, 15分钟=900, 60分钟=3600, 日线=86400
- 单例模式，全局一个TqApi连接，ws 重连由 TqSdk 内部处理
"""
import sys, os, types, logging, threading, time, re
from typing import Optional

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

# ========== 过滤 tqsdk 断线/重连噪声日志 ==========
# free-api 对有应用包的连接有 ~120s 寿命限制，掐线后 TqSdk 自动重连（对业务透明），
# 但每次都会 print "网络连接断开/重新建立/已建立" 刷屏。这里只屏蔽这三类通知，
# 认证失败等致命错误照常打印（我们自己的 TqAuthError 处理也依赖可见的错误信息）。
_TQ_NOISE_KEYWORDS = ('网络连接断开', '网络连接重新建立', '网络连接已建立')
_tq_orig_print = TqApi._print
def _tq_quiet_print(self, msg='', level='INFO'):
    try:
        if any(k in str(msg) for k in _TQ_NOISE_KEYWORDS):
            return
    except Exception:
        pass
    _tq_orig_print(self, msg, level)
TqApi._print = _tq_quiet_print

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


class TqAuthError(Exception):
    """致命错误：天勤认证失败（密码错/账号失效）。不应重试，需用户修改 secret.py 后重启服务。"""


# 启动选定的账号（server 命令行选择后注入；None=回落 secret.py 首个账户）
_ACC = {'user': None, 'pwd': None}
def set_account(user, pwd):
    _ACC['user'], _ACC['pwd'] = user, pwd


class TqData:
    """单例长连接：全局一个TqApi，WebSocket 重连由 TqSdk 内部处理（指数退避 10s→640s）。

    注意：
      1. 不要在本类里再加心跳线程 —— TqSdk 内部已自带 ws 重连，
         我们的心跳反而会推应用层包，触发 free-api 的 120s 应用包寿命限制。
      2. 一般异常路径不要 close()+new() —— TqSdk 后台正在退避重连，抢活会出乱子。
         唯一例外：交易时段内等 15s 收不到任何数据（判定连接死亡），
         由 get_kline 主动重建连接（见 _can_recreate，限频3分钟），因为
         TqSdk 的重连退避最长可达 640s，被动等会把图表卡死几分钟。
      3. 致命错误（TqAuthError）直接抛，不重试，让用户去改 secret.py。
    """
    _instance: Optional['TqData'] = None
    _lock = threading.Lock()

    # 实例属性声明：实际在 __new__ 里赋值，这里只声明类型供静态检查识别
    _api: Optional[TqApi]
    _api_lock: threading.Lock
    _klines: dict
    _auth_failed: bool
    _last_recreate: float

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._api = None
                    inst._api_lock = threading.Lock()
                    inst._klines = {}
                    inst._auth_failed = False   # 一旦认证失败置位，所有路径停止重试
                    inst._last_recreate = 0.0    # 上次主动重建连接的时间（限频用）
                    inst._hist_ok = {}           # (symbol,period) -> 订阅历史块是否完整
                    cls._instance = inst
        return cls._instance

    def _create_api(self):
        """创建新的TqApi连接（调用方必须已持有_api_lock）。"""
        if self._auth_failed:
            raise TqAuthError('天勤认证已失败，请检查 secret.py 中的账号密码并重启服务')
        # 先关闭旧连接
        if self._api:
            try:
                self._api.close()
            except:
                pass
        self._api = None
        self._klines = {}
        # 账号来源：启动命令行选择后由 server 注入（set_account）；未注入则回落 secret.py
        # （secret.py 只存数据，逻辑一律在外部——2026-10-12 用户定稿）
        user, pwd = _ACC['user'], _ACC['pwd']
        if not user:
            try:
                import secret
                accs = getattr(secret, 'ACCOUNTS', None)
                if accs:
                    user, pwd = accs[0]['user'], accs[0]['pwd']
                else:
                    user, pwd = secret.TQ_USER, secret.TQ_PASS
            except ImportError:
                raise RuntimeError(
                    '缺少 secret.py：请复制项目根目录的 secret.py.local 为 secret.py，'
                    '填入天勤账号密码（该文件不入库）')
        # 创建新连接 —— 区分认证失败（致命）vs 网络等瞬时错误（可重试）
        try:
            self._api = TqApi(auth=TqAuth(user, pwd))
        except Exception as e:
            msg = str(e)
            # 致命错误特征：天勤认证失败 / 401 / 403 / invalid_grant
            fatal_kw = ('用户权限认证失败', 'invalid_grant', 'Invalid user credentials',
                        '认证失败', 'unauthorized', '401', '403')
            if any(kw in msg for kw in fatal_kw):
                self._auth_failed = True
                raise TqAuthError(
                    f'天勤认证失败（已停止重试，请检查 secret.py 中账号密码后重启服务）: {e}'
                ) from e
            # 其他异常（网络中断等）继续向上抛，由上层重试
            raise
        print('[TqData] 天勤连接成功')
        self._last_recreate = time.time()

    def _ensure_api(self) -> TqApi:
        """懒创建 TqApi。TqSdk 内部自己处理 ws 重连，我们不抢活。"""
        if self._auth_failed:
            raise TqAuthError('天勤认证已失败，请检查 secret.py 并重启服务')
        if self._api is None:
            with self._api_lock:
                if self._api is None:
                    self._create_api()
        assert self._api is not None, '_create_api 后 _api 必非 None'
        return self._api

    def get_kline(self, symbol, period_sec, data_length=2000, force=False):
        """获取K线。

        断线韧性（free-api 会周期性掐连接，~120s 一次）：
          - TqSdk 自带重连但退避越来越长（10s→20s→…→640s），被动等可能卡几分钟；
          - 所以交易时段内若等 15s 收不到任何数据（8个品种的订阅正常时秒级来数据），
            判定连接死亡，主动 close+new 重建（新连接立即连上），限频 45s 一次。
        连续4次尝试失败则抛错，由前端5秒后自动重试。

        致命认证错误（TqAuthError）不重试，直接抛给上层。
        """
        if self._auth_failed:
            raise TqAuthError('天勤认证已失败，请检查 secret.py 并重启服务')
        tq_sym = to_tq_symbol(symbol)
        key = (tq_sym, period_sec)
        last_err = None
        df = None
        for attempt in range(4):
            try:
                self._ensure_api()
                with self._api_lock:
                    # 锁内重取实例：锁外拿到的引用可能已被其他线程重建关闭
                    # （close+new 后旧实例报 "Event loop is closed"，整轮拉取作废）
                    api = self._api
                    if key not in self._klines:
                        # 新订阅：循环等历史块真正落地（单次固定 deadline 会被其他
                        # 订阅的 tick 抢先唤醒，历史大包没到就返回 → 图表只剩几根）
                        self._klines[key] = api.get_kline_serial(tq_sym, period_sec, data_length=data_length)
                        self._hist_ok[key] = self._wait_history(api, key, tq_sym, period_sec, data_length)
                    else:
                        # 已订阅：该品种已收市（不在自己的交易时段）→ 直接回缓存
                        # （不等待、不误判连接死亡、不触发重建）
                        if self._market_idle(symbol):
                            df = self._klines[key].copy()
                            return self._to_df(df)
                        # 把网络里积累的更新收进来
                        if not api.wait_update(deadline=3):
                            # 3秒没等到任何数据 → 可能处于断线窗口，再等15s骑过重连
                            if not api.wait_update(deadline=15) and self._can_recreate():
                                # 交易时段内15s收不到任何数据：连接大概率已死（重连退避可达640s），
                                # 主动重建连接立即恢复。close+new 会清空 _klines，下面重新订阅。
                                print(f"[TqData] {time.strftime('%m-%d %H:%M:%S')} 连接疑似死亡（15s无任何数据），主动重建连接")
                                self._create_api()
                                api = self._api
                                self._klines[key] = api.get_kline_serial(tq_sym, period_sec, data_length=data_length)
                                self._hist_ok[key] = self._wait_history(api, key, tq_sym, period_sec, data_length)
                    # 历史完整性校验：free-api 长连接上订阅时历史块偶发丢失（丢了不补），
                    # serial 里只剩 live/forming 几根，_to_df 滤掉 NaN 后图表只剩一根
                    # （30秒级每次打开页面都是新订阅，最容易中招，2026-10-09）。
                    # 探测到缺失 → 重建连接重订（受 45s 限频；本轮探测失败先回旧数据，
                    # 下轮拉取自动再试，自愈）。
                    _filled = int(self._klines[key]['open'].notna().sum())
                    _suspect = not self._hist_ok.get(key, True)
                    if (_filled < 50 or _suspect) and data_length >= 200 and self._can_recreate(force):
                        # 同一轮内最多重试 2 次重建（连接刚重建时历史块下载容易再失败，
                        # 多试一次显著提高落地率——2026-10-09 白糖30秒实测）
                        for _try in range(2):
                            print(f"[TqData] {time.strftime('%m-%d %H:%M:%S')} {tq_sym} {period_sec}s 历史块缺失（仅{_filled}根，force={force}，第{_try+1}次重建）")
                            self._create_api()
                            api = self._api
                            self._klines[key] = api.get_kline_serial(tq_sym, period_sec, data_length=data_length)
                            self._hist_ok[key] = self._wait_history(api, key, tq_sym, period_sec, data_length)
                            _filled = int(self._klines[key]['open'].notna().sum())
                            if _filled >= _target_ok(data_length):
                                break
                    df = self._klines[key].copy()
                return self._to_df(df)
            except TqAuthError:
                # 致命错误：不再重试，直接抛出
                raise
            except Exception as e:
                last_err = e
                print(f'[TqData] 拉数据失败（第{attempt+1}次）: {type(e).__name__}: {e}')
                if attempt < 3:
                    time.sleep(1 + attempt * 2)   # 1s/3s/5s
        raise RuntimeError(f'拉数据失败，已重试4次: {last_err}')

    def _market_idle(self, symbol):
        """该品种当前不在自己的交易时段（trading_sessions.json 品种级时间表）
        → 此刻不可能有新数据，直接回缓存。未收录品种保守返回 False（维持原行为）。"""
        try:
            import trading_time
            import re
            prod = re.sub(r'[0-9]+', '', symbol).upper()
            return not trading_time.product_in_session(prod)
        except Exception:
            return False

    def _wait_history(self, api, key, tq_sym, period_sec, data_length, max_wait=15):
        """订阅后循环等历史块真正落地，返回 True=历史完整 / False=可疑（不完整）。\n\n        长连接上其他订阅的 tick 会持续唤醒 wait_update——固定 deadline 的单次等待\n        会在历史大包到达前就返回，serial 里只剩 live 几根（30秒级新订阅只剩1-2根\n        的根因）。循环处理事件直到：有效根数过半（正常）、或连续多次无增长\n        （品种历史本来就短，次新合约等）。"""
        deadline = time.time() + max_wait
        target = _target_ok(data_length)
        last, stable = -1, 0
        while time.time() < deadline:
            api.wait_update(deadline=1)
            serial = self._klines.get(key)
            filled = int(serial['open'].notna().sum()) if serial is not None else 0
            if filled >= target:
                return True
            if filled == last:
                stable += 1
                if stable >= 3 and filled > 0:
                    return filled >= target   # 已稳定：历史就这么多（次新合约等）
            else:
                stable = 0
            last = filled
        serial = self._klines.get(key)
        filled = int(serial['open'].notna().sum()) if serial is not None else 0
        return filled >= target

    def _can_recreate(self, force=False):
        """交易时段内且距上次重建超过45s才允许重建（防止反复close+new抖动）。\n        force=True（用户手动 ⟳ 强制刷新）跳过 45s 限频，时段校验保留。

        额外窗口限制（2026-10-08 夜盘首跑发现）：01:00-08:55 禁止重建——
        我方 17 个品种里夜盘最晚的（沪镍）01:00 收市，此后所有订阅都无行情，
        15s 无数据是**正常**的而非连接死亡（全局 SESSIONS 却到 02:35 才结束，
        是所有品种的并集）；此时重建纯属误伤，还会引发 free-api 对频繁重连的
        反向限流，形成死亡↔重建的恶性循环。"""
        try:
            import trading_time
            if not trading_time.is_trading_time():
                return False
        except Exception:
            pass   # 判断失败时保守允许重建（限频仍生效）
        try:
            if not trading_time.any_product_in_session():
                return False   # 所有已收录品种均已收市（全局时段表是并集，含空窗）
        except Exception:
            pass
        # 限频45s：实盘时段free-api约每2分钟掐一次线，tqsdk自身重连失败时
        # 靠主动重建兜底，最坏冻结=检测13s+限频等待+重建3s ≈ 1分钟内
        if force:
            return True
        return time.time() - self._last_recreate > 45

    @staticmethod
    def _to_df(serial):
        """tqsdk serial -> 清洗后的 DataFrame。"""
        df = serial
        df['datetime'] = pd.to_datetime(df['datetime'], unit='ns', utc=True).dt.tz_convert('Asia/Shanghai')
        df = df[['datetime', 'open', 'high', 'low', 'close', 'volume']].dropna(subset=['open'])
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
def _target_ok(data_length):
    return max(data_length // 2, 100)


def fetch_kline(symbol, period_sec, data_length=2000, force=False):
    return TqData().get_kline(symbol, period_sec, data_length, force=force)


if __name__ == '__main__':
    print('测试天勤数据源...')
    df = fetch_kline('CF2701', 900, 5)
    print(df.to_string())
    print(f'共 {len(df)} 根')
