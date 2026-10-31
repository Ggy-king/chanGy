# chanGy — 缠论期货看盘与预警系统

> 单机全链路：天勤行情 → 策略引擎（缠论形态识别）→ FastAPI/WebSocket 实时推送 → PC / 移动端 / Android APK 三端看盘。
> 一个人的项目，AI 协作开发模式落地：需求规格化 → AI 实现 → 体感验收 → 缺陷证伪，持续迭代中。

<!-- 截图位：docs/screenshots/matrix.png / cockpit.png / detail.png（待补） -->

## 功能总览

- **缠论形态引擎**：K线包含合并 → 顶底分型 → 笔 → 中枢 → 买卖点（A类趋势反转确认/触发、B类三买三卖），多空双向、15 品种 × 7 级别矩阵式监控
- **实时预警**：形态信号 WebSocket 弹卡（跨浏览器去重）、价格预警线、持仓确认笔强制提醒
- **持仓驾驶舱**：三价出场体系（硬止损/保本/止盈锚）、判决提示、平仓阻尼拦截、逐槽位差分渲染
- **复盘审计**：信号流 × 交易流水 diff 自动审计、周报分析（LLM 接入可选）
- **多机同步**：Gitee 私有仓库作数据权威源，PC ↔ 手机端持仓状态双向同步
- **系统自检**：`/pc/health` 全绿自检页（天勤连接/日历/数据文件/同步链路等 9 项零网络检查）

## 架构

```
天勤 TqSdk（行情） ──┐
akshare（手续费）  ──┤
                    ▼
            engine.py 策略引擎
   chanlun/（合并→分型→笔→中枢→买卖点）
                    ▼
        server.py（FastAPI 单端口）
   ├─ /api/*       REST（行情/持仓/复盘/审计）
   ├─ /ws          WebSocket 实时推送
   ├─ /pc/*        PC 端 8+ 页面（原生 JS + lightweight-charts）
   └─ /m/*         移动端页面
                    ▼
   data/*.json 运行时状态（自动重建，不入库）
```

## 快速开始

```bash
# 1. 环境（Python 3.12+）
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 天勤账号（免费注册：https://www.shinnytech.com/tqsdk/）
cp secret.py.local secret.py    # 填入你的天勤账号密码

# 3. 启动
python server.py                # 或 -y 跳过交互菜单
# 打开 http://127.0.0.1:8000
```

可选配置：
- `data/gitee_conf.example.json` → 复制为 `gitee_conf.json` 并填入你自己的 Gitee 私有仓库信息，启用 PC/手机持仓同步
- 首次运行后 `data/` 会自动生成运行时状态文件（K线缓存、监控开关等）

## 测试

```bash
.venv/bin/python tests/test_hotkeys.py        # Playwright E2E，需先安装 playwright + chromium
```

## 免责声明

本项目仅用于**个人学习与技术实践**，不构成任何投资建议。期货交易风险极高，多数参与者长期亏损——请先用模拟盘。作者不对任何直接或间接损失负责。

## 技术栈

`Python 3.12` `FastAPI` `WebSocket` `天勤 TqSdk` `akshare` `lightweight-charts` `原生 JS` `Playwright` `Android WebView 离线壳`

## License

MIT
