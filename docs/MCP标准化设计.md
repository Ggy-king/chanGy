# MCP 标准化设计（v0.2 定稿）

> 定位：把 Agent 工具链标准化为 MCP（Model Context Protocol）server——四大模块之三。
> v0.2 已锁定全部关键决定（见文末），进入施工阶段。

---

## 1. 背景与动机

主控台愿景：留言窗口是唯一操作入口——自然语言进、意图路由、调本地 Agent 工具、回执。
当前工具链 7 个已在 function calling 注册表里运转，但它们只服务于**进程内的**周复盘 Agent。
MCP 解决的是"工具以什么标准对外暴露"：

1. **主控台铺路**：意图路由调用的工具走标准化接口，新增操作（查持仓/设线/改开关）零成本挂载
2. **生态接入**：任何 MCP 客户端（Claude Desktop / Cursor 等）配置一行 JSON 即可连接本系统——
   查缠论知识库、查复盘统计、查预警状态。开源项目从"能看"变成"能接上用"
3. **架构统一**：function calling（进程内调用）与 MCP（跨进程标准协议）共用同一批底层函数，
   两个门面一套内核

## 2. 概念边界：Tool / Skill / Resource

| 概念 | 本质 | 本期做不做 |
|---|---|---|
| **Tool** | 可执行函数：名字 + 参数 schema + 本地执行 | ✅ 核心交付 |
| **Resource** | 只读数据源（文件/记录内容） | 二期评估 |
| **Prompt** | 预置提示词模板 | 不做（prompts.py 是内部资产） |
| **Skill** | 操作手册+脚本包，教会 Agent 一类任务怎么流程化执行——它是知识的组织方式，内部可调工具，但本身不可执行 | 不做（与 MCP 是两层概念，勿混） |

## 3. 工具盘点：7 个全部本地自写，零新逻辑

现有注册表（`agent/tools.py`）里的工具均为本系统数据封装函数，MCP 只是同一批函数的标准化对外暴露：

| 工具 | 数据源 | MCP 暴露 |
|---|---|---|
| get_week_overview | reviews.json + 交易流水 | ✅ |
| query_trades | history.json | ✅（脱敏沿用 redact_trades 收口） |
| calc_stats | 交易流水分组统计 + t 检验 | ✅ |
| query_audit | 审计条目 | ✅（脱敏沿用） |
| query_questions | 体系疑问清单 | ✅ |
| get_user_notes | 三问回答/行动项 | ✅（脱敏沿用） |
| kb_search | rag/ 双层阅览室 | ✅ |

数据脱敏沿用现有收口原则：时间到小时、无资金总量——MCP 门面不得绕过 `agent/tools` 的脱敏层。

**外部生态工具（client 模式）只接一个**：`fetch`（网页抓取）——补上"盘后资讯/公告查询"这个
系统没有的能力。不接的：memory（reviews.json 已是记忆，重复）、filesystem（外部程序才需要）、
其余搜索类（引入外部 API key 与数据出境，与项目私有化原则冲突）。**不为接而接**。

## 4. 架构：server 模式（对外）+ client 模式（对内）

```
mcp_server/
  __init__.py
  server.py        # FastMCP：7 个只读工具标准化暴露（stdio 传输）
  client.py        # MCP client 封装：主控台意图路由可调用的外部工具（一期仅 fetch，可选）
```

- **server 模式**：`python -m mcp_server.server` 跑 stdio 传输——Claude Desktop / Cursor 的
  `claude_desktop_config.json` 加一条配置即接入。工具集 = 上表 7 个，**全部只读**。
- **client 模式（已实现，v0.3）**：`mcp_server/client.py`——我们当 client 连外部 MCP server，
  把外部工具动态翻译成本地注册表格式 (schemas, executors)，与函数工具合并进同一张菜单。
  一期只接官方 fetch（`uvx mcp-server-fetch`，免费无 key）：盘后资讯/网页抓取。
  模型只看到一张扁平菜单，不感知工具是本地函数还是跨进程协议。
  边界：不挂进周复盘 Agent（7 封顶不变）；供留言窗口/主控台合并使用。
- **底层复用**：MCP 工具函数直接调用 `agent/tools` 的现有实现（`_kb_lookup`、
  `build_registry` 的执行器），不复制业务逻辑。

## 5. 安全边界

1. **MCP 门面默认只读**：写操作（设预警线/取消/改监控开关）**不暴露**——MCP 客户端是外部程序
   （别人的模型在驱动），写操作的鉴权与确认必须留在本地留言口（确定性规则 + 人工确认）。
   「LLM 应用的安全边界永远放在代码里」在 MCP 场景的投影：**跨进程暴露面只开读，不开写**。
2. **脱敏收口唯一**：所有经 MCP 出去的数据走 `agent/tools` 现有脱敏函数，不得新增绕过路径。
3. **工具数量纪律**：单 Agent 工具集 ≤ 10 个（超过 10-15 个模型选工具准确率下降）。
   当前 7 + 主控台规划 3（设线/取消/持仓查询，走本地不走 MCP）= 10 封顶。

## 6. 验证方式

1. 本地起 `python -m mcp_server.server`，Claude Desktop 配置 JSON 接入，实测三轮对话：
   查知识库（带课号出处）/ 查本周统计（带脱敏口径）/ 查体系疑问
2. 公开库 README 增加「MCP 接入」章节（配置示例），作为开源项目亮点
3. `tests/` 增加 MCP 工具注册断言（与 function calling 注册表同源校验——
   同一批函数，两个门面，防止单边漂移）

## 7. 施工步骤（预估 1-1.5 天业余时间）

| # | 步骤 | 产出 |
|---|---|---|
| 0 | fastmcp 依赖引入（requirements.txt） | 依赖就绪 |
| 1 | mcp/server.py：7 工具标准化暴露（复用脱敏层） | MCP server 可跑 |
| 2 | Claude Desktop 实测接入 + 三轮对话验证 | 接入配置示例（进 README） |
| 3 | tests/ 注册表同源断言 | 防漂移测试 |
| 4 | 公开库 README「MCP 接入」章节 | 开源亮点落地 |

## 8. 已锁定决定（v0.2）

1. **传输方式：一期只做 stdio**——client（Claude Desktop / Cursor 等）与 server 同机，管道通信、
   不经网络。HTTP 留二期：那是"client 出现在其他设备"时才需要造的门（开端口/鉴权/防滥用），
   判断标准 = client 在哪，没有远程调用需求就不开网络暴露。
2. **写操作不进 MCP**——MCP 门面只读。本地只说明传输不经网络，不说明调用者可信：
   client 背后是外部模型在决策，"人工确认"环节不在系统控制内。读错只误导对话，
   写错直接变成真金白银的状态变更——读写风险不对称，只读边界在本地 MCP 依然成立。
3. **client 模式（fetch）一期不做**——先交付 server，主控台对资讯查询的实际使用频率
   决定它值不值后续工作量。（v0.3 兑现：fetch 桥已落地，触发条件=主控台资讯需求明确）
4. **交付物进公开库**——MCP server + README「MCP 接入」章节。

## 变更记录

- v0.1 —— 初稿对齐（暴露范围 / Tool-Skill 概念边界 / 工具数量纪律）
- v0.2 —— 四项关键决定锁定（stdio / 只读 / 无 client / 进公开库），进入施工
- v0.3 —— client 模式落地：`mcp_server/client.py` fetch 桥（动态清单防漂移 / 每调用独立会话 /
  6000 字截断 / 重名即拒 / 无 uvx 明确报错），tests 3 项真实网络实测。
  留言窗口触发词接入待拍板（产品行为变更，单独决策）
