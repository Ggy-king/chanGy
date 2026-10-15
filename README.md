# 缠论期货看盘与预警系统（自用说明）

> 本地跑、单机单端口、PC + 手机页面、FastAPI + WebSocket 双向通信。
> 数据源：天勤 TqSdk（免费、不限调用次数、支持任意周期），列表页最新价走新浪，手续费走 akshare。
> 算法骨架：K线包含合并 → 顶底分型 → 笔 → 中枢 → 买卖点（A类趋势反转 / B类三买三卖），多空双向监控。

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

启动后出现**命令行账户菜单**（↑↓ 箭头键 + 回车选择，来自 `secret.py` 的 `ACCOUNTS` 列表；
选过的账户记忆在 `data/last_account.json`，下次回车默认选中项；`py server.py -y` 跳过交互用默认）。
非交互环境（脚本/自启）自动用默认账户，不卡菜单。

浏览器打开：
- 电脑矩阵（主页面）：http://localhost:8000/pc/matrix
- 电脑总览：http://localhost:8000/pc
- 手机列表：http://<局域网IP>:8000/m （手机连同一个 WiFi）

停止：控制台 Ctrl+C，或关掉窗口。

---

## 二、目录结构

```
chanGy/
├─ server.py             ★服务入口：FastAPI 路由 + WebSocket + 形态监控循环 + 持仓Gitee云同步
├─ engine.py             ★行情引擎：拉数据→缠论结构→买卖点→回测统计→风控手数
├─ config.py             ★全局配置：合约清单、级别、风控参数（最常改）
├─ secret.py             天勤账户配置（git忽略，切勿入库）：ACCOUNTS 多账户列表
├─ fees.py               手续费/乘数/最小跳动（akshare 拉取，启动3分钟自动刷新）
├─ backtest.py           独立回测引擎（PC回测页调用，不影响实时预警）
├─ backtest_v3.py        回测 v3
├─ alert_manager.py      ★价格预警管理：增删改查、触发判断、持久化
├─ tqsdk_data.py         ★天勤 TqSdk 数据源封装：单例长连接 + 订阅缓存 + 自动重连
├─ trading_time.py       交易日历 + 交易时段判断（节假日/非交易时段零拉取）
│
├─ chanlun/              ★缠论算法包
│  ├─ kline_merge.py      K线包含处理（双向包含，chan.py原版）
│  ├─ fenxing.py          顶/底分型识别（严格分型）
│  ├─ bi.py               笔的构建（含 allow_rollback 笔破坏回退开关）
│  ├─ zs.py               中枢
│  ├─ pattern.py          形态机：A类VREV 反转 / B类三买三卖，候选→确认→触发状态机
│  └─ __init__.py
│
├─ web/                  前端页面（纯 HTML + lightweight-charts 4.1.3 CDN）
│  ├─ pc_matrix.html      ★形态监控矩阵：品种×级别格子、实时事件卡、持仓状态条
│  ├─ pc_overview.html    PC总览：品种状态、总胜率/盈亏比/回撤、实时信号流、预警列表
│  ├─ pc_detail.html      PC详情：K线+笔+箭头、多级别切换、价格预警、止损计算器、⚙笔设置
│  ├─ pc_positions.html   持仓驾驶舱：三价离场（硬止损/保本/锚）、升级判决、对账单
│  ├─ pc_stats.html       统计页
│  ├─ pc_backtest.html    PC回测：选品种+时间段，独立跑回测，权益曲线
│  ├─ pc_help.html        帮助页
│  ├─ m_list.html         手机端：信号列表 + 预警列表（震动响铃）
│  ├─ m_detail.html       手机端：单信号详情
│  ├─ popup_cards.js      右上角弹卡组件（预警/持仓共用）
│  ├─ positions_core.js   持仓面板共用逻辑
│  └─ struct_zone.js      结构区组件
│
├─ data/                 运行时数据（哪些进 git 见第八节）
├─ docs/                 设计文档（持仓离场预警系统设计.md 等）
├─ mobile-app/           安卓 APK 工程（源码入库，构建产物忽略）
├─ requirements.txt      干净环境的 pip 依赖清单
├─ README.md             本文件
└─ .gitignore
```

---

## 三、最常改的东西，在哪改

### 1. 换主力合约 / 增删品种 → `config.py` 的 `CONTRACTS`
主力合约换月只改这里的 `symbol`。大小写按天勤规则：**郑商所3位月份大写（CF701），其他4位月份小写（jm2701/rb2701）**。
改完 `config.CONTRACTS` 会自动落成 `data/contracts.json` 并推 Gitee（换月后旧合约的历史事件卡自动不复活）。

### 2. 天勤账户 → `secret.py` 的 `ACCOUNTS`
```python
ACCOUNTS = [
    {"name": "主账户", "user": "xxx", "pass": "xxx"},
    {"name": "friend", "user": "xxx", "pass": "xxx"},   # 朋友的账户，填好即出现在启动菜单
]
```
此文件 **git 忽略，永不入库**。

### 3. 级别参数 → `config.py` 顶部；风控参数 → `config.py` 底部（同旧版）

### 4. 笔的配置（六开关）→ 详情页/矩阵页 ⚙ 面板
前端改动会 POST 到 `/api/pattern_bi_conf`，服务端持久化到 `data/pattern_bi_conf.json`
并**静默重扫全量重建基线**（约 18 秒/85 单元）。每台机器的浏览器 localStorage 各自记忆。

### 5. 监控总开关 → 矩阵页 ⚙ 设置
只管形态监控（pattern_loop + periodic）；价格预警与手续费刷新是看盘配套，永远运行。
状态持久化 `data/monitor_switch.json`。

---

## 四、页面与路由

| 路径 | 作用 |
|---|---|
| `/` | 自动跳 `/pc/matrix` |
| `/pc/matrix` | ★形态监控矩阵（品种×级别格子 + 实时事件卡 + 持仓状态条） |
| `/pc` | PC总览（品种状态 + 实时信号流 + 预警列表） |
| `/pc/detail?sym=jm2701` | PC单品种详情（多级别看盘 + 预警 + 止损计算器 + ⚙笔设置） |
| `/pc/positions` | 持仓驾驶舱（三价离场 + 升级判决 + 平仓对账单） |
| `/pc/stats` | 统计页 |
| `/pc/backtest` | PC回测页 |
| `/pc/help` | 帮助页 |
| `/m`、`/m/detail` | 手机端列表/详情 |
| `/api/pattern_matrix` | 矩阵全量状态 + 事件流 |
| `/api/exit_hint` | 持仓离场三价提示（硬止损/保本/锚 + 中枢边界判定） |
| `/api/positions*` | 持仓增删改查（Gitee 云同步） |
| `/api/alerts*` | 价格预警增删改查 |
| `/api/live` | 单品种单级别 K线 + 笔 + 买卖点 |
| `/ws?role=pc\|m` | WebSocket 双向频道 |

---

## 五、形态监控矩阵（核心功能）

- **格子** = 品种×级别单元，状态机：观察 → 候选 → 确认/触发 → 作废/消退，实时可逆。
- **A类（VREV）**：趋势反转结构，全量重放评估——每次对齐扫描重放回看窗口内全部历史转移。
- **B类**：三买/三卖，只评尾部。
- **事件卡片**：卡片是"事件留底"，按结构指纹原位更新、不消失（候选/确认各一张独立卡）。
  事件流容量 300，满员按**触发时间**淘汰最老卡（用户定稿 2026-10-15）；
  每日推送量统计在跨天时打印 `[事件流统计]`，容量按"两个交易日推送量"回检。
- **扫描节奏**：3/5/15/30/60 分级别按挂钟对齐扫描，收盘 +3 分钟补扫最后一根K线；
  合盖休眠超 4 分钟恢复后立即补扫。
- **推送**：新事件右上角弹卡 + 声音（确认/触发与候选分开调音）；确认/触发卡带"已成笔/未成笔"徽标。

---

## 六、持仓系统（驾驶舱 + 云同步）

- 持仓录入后：矩阵页持仓状态条 + 驾驶舱页双入口。
- **三价离场**：硬止损 / 保本 / 锚（浮盈保护），叠加本级别反向结构与更大级别同向确认做"绿灯/黄灯"判决。
- **云同步**：Gitee 私有仓库 = 唯一权威数据源，三件套 `positions.json`（当前持仓）、
  `history.json`（平仓简报）、`contracts.json`（合约清单）双向合并同步；
  凭证在 `data/gitee_conf.json`（git 忽略）。手机端平仓 → 电脑可见。

---

## 七、价格预警（右键K线图）与一键止损线

- 预警存 `data/alerts.json`（git 跟踪，换电脑都在）；触发后右上角弹卡 + 手机震动响铃。
- 一键止损线存 `data/sl_lines.json`，每品种一条通用。

---

## 八、data/ 文件清单与 git 策略

**原则：跨机器有意义的用户数据 / 静态配置 → git 跟踪；本机运行时状态（自动重建）→ git 忽略。**

| 文件 | 是什么 | git |
|---|---|---|
| `positions.json` / `history.json` / `contracts.json` | 持仓/平仓简报/合约清单（Gitee 云同步三件套） | 跟踪 |
| `alerts.json` | 价格预警设置 | 跟踪 |
| `sl_lines.json` | 一键止损线 | 跟踪 |
| `pattern_bi_conf.json` | 笔六开关（本机浏览器会覆盖推送） | 跟踪 |
| `trading_sessions.json` | 交易时段配置（静态） | 跟踪 |
| `pattern_snapshot.json` | 形态监控事件流快照（每轮扫描重写，重启自动重建） | 忽略 |
| `fees_cache.json` | 手续费缓存（启动自动联网刷新） | 忽略 |
| `.stats_cache.json` | 统计缓存（自动重建） | 忽略 |
| `trading_days_cache.json` | 交易日历缓存（自动从新浪重建） | 忽略 |
| `last_account.json` | 本机选的账户记忆 | 忽略 |
| `monitor_switch.json` | 本机监控总开关状态 | 忽略 |
| `pattern_flags.json` | 本机反向中枢开关 | 忽略 |
| `history.json.bak` | 同步合并备份 | 忽略 |
| `gitee_conf.json` | Gitee 凭证（凭证类，永不入库） | 忽略 |
| `logs/` | 运行日志 | 忽略 |

> 忽略 ≠ 删除：本地文件原样保留，只是不再进 git/GitHub。缓存类文件在新机器上首次运行自动重建。

---

## 九、依赖与环境

- **Windows**：Python 3.12，用 `py` 启动；**macOS**：Python 3.12，依赖装在 `.venv/`
- **天勤账号**：`secret.py` 配置（免费注册，支持任意周期，不限调用次数）
- **前端**：lightweight-charts 4.1.3 CDN，首次打开需联网
- **全新机器**：`pip install -r requirements.txt`

---

## 十、其他命令

```bash
# mac：释放端口
lsof -ti:8000 | xargs kill -9

# windows：释放端口
netstat -ano | findstr :8000
taskkill /F /PID 12345

# 改了 chanlun 后清理缓存
rm -rf __pycache__ chanlun/__pycache__
```
