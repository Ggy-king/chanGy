# 缠论期货量化预警系统（自用说明）

> 本地跑、单机单端口、PC + 手机五页面、FastAPI + WebSocket 双向通信。
> 数据源：天勤 TqSdk（免费、不限调用次数、支持任意周期），列表页最新价走新浪。
> 策略骨架：K线包含合并 → 顶底分型 → 笔；只做多，底分型买、顶分型平。

---

## 一、怎么启动

### Windows（本机一律用 `py`，不是 `python`）

```bat
:: 方式一：双击
启动预警服务.bat

:: 方式二：命令行
cd /d E:\agent\chanGy
py server.py
```

### macOS（首次使用：装一次环境，以后不用再装）

```bash
cd ~/Desktop/chanGy
brew install python@3.12                       # 没有 3.12 时才需要
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### macOS（日常启动：三行）

```bash
cd ~/Desktop/chanGy
source .venv/bin/activate
python server.py
```

> - 懒得激活的等价写法：`.venv/bin/python server.py`
> - `.venv/` 已加入 .gitignore

启动后控制台打印：
- 电脑总览：http://localhost:8000/pc
- 手机列表：http://<局域网IP>:8000/m （手机连同一个 WiFi）

浏览器打开 `/` 自动跳 `/pc`。**手机和电脑连同一个 WiFi**。

停止：控制台 Ctrl+C，或关掉窗口。

---

## 二、目录结构

```
E:\agent\chanGy\
├─ server.py              ★服务入口：FastAPI 路由 + WebSocket + 定时刷新 + 预警检查
├─ engine.py             ★行情引擎：拉数据→缠论结构→买卖点→回测统计→风控手数
├─ config.py             ★全局配置：合约清单、级别、账户/风控参数（最常改）
├─ fees.py               手续费/乘数/最小跳动：启动超30天自动联网更新
├─ backtest.py           独立回测引擎（PC回测页调用，不影响实时预警）
├─ alert_manager.py      ★价格预警管理：增删改查、触发判断、持久化
├─ tqsdk_data.py         ★天勤 TqSdk 数据源封装：单例长连接 + 订阅缓存 + 自动重连
├─ trading_time.py       交易日历 + 交易时段判断（节假日/非交易时段不刷新）
├─ 启动预警服务.bat       双击启动
│
├─ chanlun/              ★缠论算法包（以后加线段、中枢都放这里）
│  ├─ kline_merge.py      K线包含处理（双向包含，chan.py原版）
│  ├─ fenxing.py         顶/底分型识别（严格分型）
│  ├─ bi.py              笔的构建（含 allow_rollback 笔破坏回退开关）
│  └─ __init__.py
│
├─ web/                  前端页面（纯 HTML + lightweight-charts 4.1.3 CDN）
│  ├─ pc_overview.html    PC总览：17品种状态、总胜率/盈亏比/回撤、实时信号流、预警列表
│  ├─ pc_detail.html      PC详情：K线+笔+箭头、多级别切换、价格预警、止损计算器、买卖点历史
│  ├─ pc_backtest.html    PC回测：选品种+时间段，独立跑回测，权益曲线
│  ├─ m_list.html         手机端：信号列表 + 预警列表（震动响铃）
│  └─ m_detail.html       手机端：单信号详情（开仓区间/止损/建议手数/策略理由）
│
├─ data/                 运行时数据（全部git跟踪，换电脑clone下来直接能用）
│  ├─ alerts.json         价格预警设置（换电脑都在）
│  ├─ fees_cache.json     手续费缓存
│  ├─ .stats_cache.json   回测统计缓存
│  └─ trading_days_cache.json  交易日历缓存
│
├─ pylibs/               ⚠第三方库便携副本（已gitignore，不进GitHub，不要手工改）
├─ requirements.txt      干净环境的 pip 依赖清单
├─ README.md             本文件
└─ .gitignore
```

---

## 三、最常改的东西，在哪改

### 1. 换主力合约 / 增删品种 → `config.py` 的 `CONTRACTS`
主力合约换月只改这里的 `symbol`。大小写按天勤规则：**郑商所3位月份大写（CF701），其他4位月份小写（jm2701/rb2701）**。
```python
CONTRACTS = [
    {"symbol": "jm2701", "name": "焦煤", "exchange": "DCE"},
    {"symbol": "CF2701", "name": "棉花", "exchange": "CZCE"},
    ...
]
```

### 2. 级别参数 → `config.py` 顶部
```python
PERIOD_SEC   = "3"     # 次级别
PERIOD_MAIN  = "15"    # 本级别（当前预警级别）
PERIOD_BIG   = "60"    # 更大级别
```
详情页支持手动切换：30秒 / 1分 / 3分 / 15分 / 60分 / 日线。

### 3. 模拟账户 / 风控参数 → `config.py` 底部
```python
ACCOUNT_CAPITAL  = 1000000  # 初始资金
RISK_PER_TRADE   = 0.01     # 单笔最大风险 1%
ENTRY_RANGE_TICKS = 2       # 建议开仓区间上下各N跳
MARGIN_RATE      = 0.10     # 保证金率估算
```

### 4. 改缠论"笔"的算法 → `chanlun/bi.py`
- 双向包含（chan.py原版），已稳定
- `allow_rollback` 参数：笔破坏回退开关，详情页头部"回退"按钮切换
- 改完必须：删 `chanlun/__pycache__` → 重启 → 多品种比对前后笔数

### 5. 改买卖点逻辑（策略）→ `engine.py` 的 `_build()`
- 缠论结构计算 → 买卖点生成 → 统计 → 风控手数

### 6. 价格预警 → `alert_manager.py`
- 预警持久化到 `data/alerts.json`
- 触发逻辑：up方向 `last < price <= cur`，down方向 `last > price >= cur`
- 触发后通过WebSocket推送到PC和手机

---

## 四、价格预警功能（右键K线图）

1. **添加预警**：右键K线图 → "添加价格预警" → 鼠标变十字星 + 黄色临时线跟随 → 点击确认
2. **方向自动判断**：预警价 > 当前价 → 上穿（红色，等涨破）；预警价 < 当前价 → 下穿（绿色，等跌破）
3. **拖动调整**：直接拖动预警线改变价格，拖完方向自动重新判断，自动保存
4. **删除**：右键预警线 → 自定义确认弹窗 → 删除
5. **触发推送**：价格穿越预警线后 → 线自动消失 → 手机震动响铃 → PC弹窗（手机不在线时）→ 实时信号流显示
6. **换电脑**：预警存在 `data/alerts.json`，git跟踪，clone下来都在

---

## 五、服务路由

| 路径 | 作用 |
|---|---|
| `/` | 自动跳 `/pc` |
| `/pc` | PC总览（品种状态 + 实时信号流 + 预警列表） |
| `/pc/detail?sym=jm2701&period=15` | PC单品种详情（多级别看盘 + 预警 + 止损计算器） |
| `/pc/backtest` | PC回测页（选品种 + 时间段） |
| `/m` | 手机信号列表 + 预警列表 |
| `/m/detail?sym=...&side=buy&price=...` | 手机单信号详情 |
| `/api/overview` | 全部品种统计 JSON |
| `/api/detail?sym=...` | 单品种完整快照 JSON |
| `/api/live?sym=...&period=15&rollback=1` | 单品种单级别K线 + 笔 + 买卖点 |
| `/api/backtest?sym=...&months=3` | 回测数据 |
| `/api/alerts` (GET/POST) | 预警列表 / 添加预警 |
| `/api/alerts/{id}` (PATCH/DELETE) | 更新 / 删除预警 |
| `/api/alerts/clear_triggered` (POST) | 清空已触发预警 |
| `/api/quotes` | 列表页最新价（新浪源） |
| `/api/contracts` | 合约清单 |
| `/api/trading_status` | 当前是否交易时段 |
| `/ws?role=pc\|m` | WebSocket双向频道 |

**刷新策略**：
- 详情页：每30秒刷新当前品种当前级别
- 列表页：交易时段内每15分钟刷新全部品种，非交易时段不刷新
- 预警检查：每次刷新时同步检查

---

## 六、交易时段

```python
SESSIONS = [
    (8, 55, 11, 35),    # 上午（提前5分钟开始，延后5分钟结束）
    (13, 25, 15, 5),    # 下午
    (20, 55, 23, 59),   # 夜场上半段
    (0, 0, 2, 35),      # 夜场下半段（凌晨）
]
```
- 节假日、周末不自动刷新
- 首次进入详情页仍会拉数据（不受时段限制）
- 交易日历自动从新浪拉取，缓存到 `data/trading_days_cache.json`

---

## 七、依赖与环境

- **Windows**：Python 3.12，用 `py` 启动，第三方库走 `pylibs/` 便携目录
- **macOS**：Python 3.12，依赖装在 `.venv/`，启动前 `source .venv/bin/activate`
- **天勤账号**：免费注册，代码中已内置，支持任意周期（秒数表示），不限调用次数
- **numpy冲突修复**：`pylibs/pandas/compat/pyarrow.py` 已修改禁用pyarrow，重装pandas会丢失需重改
- **前端**：lightweight-charts 4.1.3 CDN，首次打开需联网
- **全新机器**：`pip install -r requirements.txt`

---

## 八、已完成功能

- ✅ 缠论三层算法：K线双向包含合并 / 严格顶底分型 / 笔（含笔破坏回退开关）
- ✅ 17个品种实时预警，WebSocket推送到手机和PC
- ✅ 天勤TqSdk数据源（单例长连接 + 订阅缓存 + 自动重连）
- ✅ 多级别看盘：30秒 / 1分 / 3分 / 15分 / 60分 / 日线切换
- ✅ 价格预警：右键添加、拖动调整、右键删除、触发推送、手机震动响铃
- ✅ 拖拽式止损计算器：总资金/止损比可编辑，实时显示手数/金额/点数
- ✅ 独立回测系统：选品种+时间段，权益曲线，胜率/盈亏比/回撤
- ✅ PC总览：总交易笔数/胜率/盈亏比/最大回撤/平均回撤
- ✅ 手机端：信号列表 + 预警列表，震动响铃，可删除
- ✅ 列表页拖动排序，品种名金色加粗，序号显示
- ✅ 交易时段控制，节假日不刷新
- ✅ 模拟账户：100万资金，1%风险反推手数
- ✅ 双向通信：PC和手机互发消息

---

## 九、待办（路线图）

- 线段（xianduan.py）—— 归并弱反抽小笔
- 中枢 / 次级别走势类型 / 背驰（区间套：3分→15分→60分）
- 真实结构止损（当前是最近底分型低点占位）
- 手机锁屏推送需HTTPS（当前是页内弹窗+震动）

---

## 十、其他命令

```bat
:: 释放端口
windows版
netstat -ano | findstr :8000
taskkill /F /PID 12345

mac版
lsof -ti:8000 | xargs kill -9

:: 清理缓存（改了chanlun后必做）
rmdir /s /q __pycache__ chanlun\__pycache__
```
