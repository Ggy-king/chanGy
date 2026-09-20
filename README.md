# 缠论期货预警系统（自用说明）

> 本地跑、单机单端口、PC + 手机四页面、FastAPI + WebSocket 双向通信。
> 数据源：新浪期货分钟线（akshare `futures_zh_minute_sina`），免费、免 token。
> 策略骨架：K线包含合并 → 顶底分型 → 笔；只做多，底分型买、顶分型平。

---

## 一、怎么启动

```bat
:: 方式一：双击
启动预警服务.bat

:: 方式二：命令行（注意本机一律用 py，不是 python）
cd /d E:\agent\chan
py server.py
```

启动后控制台会打印：
- 电脑总览：http://localhost:8000/pc
- 手机列表：http://<局域网IP>:8000/m （手机连同一个 WiFi）

浏览器打开 `/` 会自动跳 `/pc`。**手机和电脑连同一个 WiFi**，手机访问控制台里那行 IP:8000 即可。

停止：控制台窗口 Ctrl+C，或关掉黑窗口。

---

## 二、目录结构（每个文件/文件夹干嘛）

```
E:\agent\chan\
├─ server.py              ★服务入口：FastAPI 路由 + WebSocket + 每15分钟定时刷新
├─ engine.py             ★行情引擎：拉数据→缠论结构→买卖点→回测统计→风控手数
├─ config.py             ★全局配置：合约清单、级别、账户/风控参数（最常改的文件）
├─ fees.py               手续费/乘数/最小跳动率：启动时超30天自动联网更新，缓存到 fees_cache.json
├─ backtest_v3.py        【早期demo】甲醇 MA0 单品种离线回测脚本，独立可跑，与线上服务无关
├─ 启动预警服务.bat       双击启动的批处理
│
├─ chanlun\              ★缠论算法包（以后加线段、中枢都放这里）
│  ├─ kline_merge.py      K线包含处理（merge_klines）——已稳定，勿动
│  ├─ fenxing.py         顶/底分型识别（find_fenxing）——已稳定，勿动
│  ├─ bi.py              笔的构建（build_bi）——当前核心算法
│  └─ __init__.py        导出 merge_klines / find_fenxing / build_bi
│
├─ web\                  前端页面（纯 HTML + lightweight-charts CDN）
│  ├─ pc_overview.html    PC 总览仪表盘（17品种状态、总胜率/盈亏比/回撤、模拟买卖按钮）
│  ├─ pc_detail.html      PC 单品种详情（K线+黄笔线+买卖箭头、保证金/手续费、买卖点历史、给手机留言框）
│  ├─ m_list.html         手机端：预警信号列表
│  └─ m_detail.html       手机端：单信号详情（开仓区间/止损/建议手数/策略理由）
│
├─ pylibs\               ⚠第三方库便携副本（83MB，已 gitignore，不进 GitHub，不要手工改）
├─ fees_cache.json       运行时生成的手续费缓存（已 gitignore）
├─ requirements.txt      干净环境的 pip 依赖清单
└─ .gitignore
```

---

## 三、最常改的东西，在哪改

### 1. 换主力合约 / 增删品种 → 改 `config.py` 的 `CONTRACTS`
主力合约换月时，只改这里的 `symbol` 即可，其他文件不用动。
大小写按新浪期货规则：**郑商所大写、大商所小写、上期所大写**。
```python
CONTRACTS = [
    {"symbol": "jd2611", "name": "鸡蛋",   "exchange": "DCE"},
    {"symbol": "UR2701", "name": "尿素",   "exchange": "CZCE"},
    ...
]
```

### 2. 级别（次级别/本级别/更大级别）→ `config.py` 顶部
```python
PERIOD_SEC   = "3"     # 次级别（以后做区间套用）
PERIOD_MAIN  = "15"    # 本级别（当前预警的级别）
PERIOD_BIG   = "60"    # 更大级别
```

### 3. 模拟账户 / 风控参数 → `config.py` 底部
```python
ACCOUNT_CAPITAL = 100000   # 初始资金
RISK_PER_TRADE  = 0.01      # 单笔最大风险 1%（2% 是红线）
ENTRY_RANGE_TICKS = 2       # 建议开仓区间上下各 N 个最小跳动
MARGIN_RATE     = 0.10      # 保证金率估算
```

### 4. 改缠论"笔"的算法 → `chanlun\bi.py`
关键常量在文件顶部：
- `MIN_BI_GAP`：顶底分型中间至少隔几根合并K线
- `MIN_BREAKS`：从端点逐次新高/新低的最少台阶数
> 改完笔算法**必须**：删 `chanlun\__pycache__` → 重启服务 → 离线多品种比对前后笔数 → Edge 截图自检，确认没改坏全局。

### 5. 改买卖点逻辑（策略）→ `engine.py` 的 `_build()`
- 第 78-80 行：`merge_klines` → `find_fenxing` → `build_bi`
- 第 100-122 行：根据每一笔生成买卖点 `markers` / `signals`（up笔=底买、down笔=顶平）
- 第 141 行 `_stats()`：底买顶卖配对、胜率/盈亏比/回撤统计
- 第 46 行 `_risk_info()`：按止损距离反推建议手数、开仓区间、保证金

---

## 四、服务路由（server.py）

| 路径 | 作用 |
|---|---|
| `/` | 自动跳 `/pc` |
| `/pc` | PC 总览 |
| `/pc/detail?sym=MA2610` | PC 单品种详情 |
| `/m` | 手机信号列表 |
| `/m/detail?sym=...&side=buy&price=...&time=...&reason=...` | 手机单信号详情 |
| `/api/overview` | 全部品种统计 JSON |
| `/api/detail?sym=...` | 单品种完整快照 JSON |
| `/api/fees` | 手续费/乘数表 |
| `/api/simulate?sym=...&side=buy` | PC 端手动模拟一个信号（测试推送） |
| `/ws?role=pc\|m` | WebSocket 双向频道，信号/聊天/刷新都走这里 |

后台定时：每个 15 分整点后第 20 秒刷新全部品种；新信号才推送给在线手机/电脑。

---

## 五、依赖与环境

- Python 3.12，本机用 `py` 启动。
- `pylibs/` 是当时为了不污染 Anaconda 环境拷进来的第三方库（akshare / fastapi / uvicorn / websockets / pydantic …），**已 gitignore，不要传 GitHub**。
- 全新机器：`pip install -r requirements.txt`，并把代码里 `sys.path.insert(...'pylibs')` 那行删掉（或留着也不影响，只要本机有 pylibs）。
- 前端图表用 CDN 的 lightweight-charts 4.1.3，首次打开需联网。

---

## 六、当前口径与待办（路线图）

**已完成**
- K线包含合并 / 顶底分型 / 笔（按缠论结合律：顶底分型 + 不共用K线 + 至少1根独立K线，顶高于底）
- 17 个品种每 15 分钟拉新浪数据、自动识别新买卖点并 WebSocket 推手机
- PC 总览（总交易笔数/胜率/盈亏比/最大回撤/平均回撤）、PC 详情（K线+笔+箭头+保证金/手续费/每手乘数）
- 手机列表 + 信号详情（开仓区间±2跳、止损价、建议手数、每手保证金、单笔风险金额）
- 模拟 10 万账户、按 1% 风险反推手数

**未做（下一步）**
- 线段（xianduan.py）——用来把下跌趋势里的弱反抽小笔归并成大方向
- 中枢 / 次级别走势类型 / 背驰（区间套：3分→15分→60分）
- 真实结构止损（当前只是把最近底分型低点当止损占位）
- 手机锁屏推送需 HTTPS（当前是页内弹窗 + 震动）
