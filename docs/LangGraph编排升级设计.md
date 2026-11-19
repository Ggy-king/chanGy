# LangGraph 编排升级设计（v0.2 定稿）

> 定位：周复盘 Agent 的编排引擎换代——手写工具调用循环 → StateGraph 图编排。四大模块之四。
> v0.2 已锁定全部关键决定（见 §5），进入施工阶段。

---

## 1. 现状：编排长什么样，问题在哪

当前 Agent 的"大脑"在 `agent/llm.py::chat_tools`——一个手写的多轮循环：

```
research（研究员阶段）          coach（教练阶段）
┌─────────────────────┐        ┌──────────────┐
│ LLM 请求            │        │ 最终深谈输出   │
│  ├ 返回 tool_calls  │──执行──→│ （同一条循环  │
│  ├ 结果回灌 → 下一轮 │  工具   │   里自然发生）│
│  └ 返回正文 → 结束   │        └──────────────┘
└─────────────────────┘
```

诚实盘点：这个循环**能跑、已验证**（重试退避、思考预算兜底、工具报错回灌、12 轮封顶都在）。
它的问题不是"跑不动"，而是三件结构性的：

1. **两阶段是隐式的**：研究员→教练的切换只活在 prompt 文字里，代码层面是一个平坦循环——
   模型第几轮切阶段、研究阶段证据是否拿够，代码无法表达也无法约束
2. **状态不可恢复**：12 轮循环跑到第 9 轮进程挂了，全部重来（模型调用是成本大头）
3. **行为无回归锁**：改 prompt / 换模型 / 调轮次参数，输出质量只能靠人眼看——没有
   "改完跑一遍知道没退化"的机制

## 2. 目标架构：StateGraph 显式化两阶段

```
START → research ⇄ execute_tools ──(证据足够)──→ coach → END
              └──(轮次超限)──→ fallback(报错出口)
```

| 图元素 | 对应现有代码 | 变化 |
|---|---|---|
| State | `msgs` 局部变量 | 显式 State：messages / tools_used / rounds / citations / 阶段标记 |
| research 节点 | 循环体前半（LLM 请求 + 解析） | 一次 LLM 调用 = 一次节点执行 |
| execute_tools 节点 | `tool_fn(name, args)` 回灌 | 独立节点，工具执行与模型调用解耦 |
| 条件边 | `if calls: continue` | 显式路由：有 tool_calls → execute_tools；正文 → coach；超限 → fallback |
| coach 节点 | 无（靠 prompt 自觉） | 独立节点：research 拿够证据后触发，prompt 收口为教练阶段 |
| checkpoint | 无 | StateGraph 自带——每轮落一次状态，中断可续 |

**边界（不动的部分）**：
- `agent/prompts.py` 一字不改——图是编排层，不是 prompt 层
- `agent/llm.py` 的 `_post`（重试退避）、思考预算兜底逻辑保留——它们是模型接入层职责，
  不是编排职责；chat_tools 的循环体拆进节点后，chat_tools 降级为兼容入口或删除
- `agent/tools.py` 注册表零改动——工具层与编排层正交（MCP 已证明这层是稳定的）

## 3. 依赖成本（拍板前必须看清的账）

`langgraph` 不是单包，链条是 `langgraph → langchain-core → pydantic` 等，装下来比
`mcp` 重一个量级。对应的代价/收益：

| 方案 | 依赖 | 换来什么 |
|---|---|---|
| A. langgraph StateGraph | langgraph 全家桶 | checkpoint 免费拿、图可视化、生态兼容（后续接 evaluator/LangSmith 现成）、模块四回归体系有依托 |
| B. 自研轻量状态机（~100 行） | 零新依赖 | 够用但 checkpoint/回归/可视化全部自己写——重复造轮子，且"手写编排"正是要退出的东西 |

倾向 A：生态成熟，checkpoint / 可视化 / 评估挂点都是现成的；自研状态机会重复造轮子，
且"手写编排"正是要退出的东西。四大模块收口在编排引擎换代，模块四的回归体系需要图骨架作依托。

## 4. 验证方式：回归锁行为

1. **快照基线**：升级前用当前 chat_tools 跑 3 个历史周数据包（真实数据、脱敏口径），
   存输出快照（citations / tools_used / rounds 区间 / 深谈文本要素）作基线
2. **升级后对拍**：同一数据包跑新图，断言要素不退化（文本逐字相同不现实——模型非确定性，
   锁的是结构性指标：证据编号格式、工具调用次数区间、禁令条款触发数）
3. **E2E**：`/api/agent/weekly` 全链路（pack 构建 → 图执行 → reviews.json 落库 → 前端证据链回查）
4. **中断恢复**：跑到第 N 轮 kill 进程，重启后从 checkpoint 续跑——checkpoint 价值的实证

## 5. 关键决定（v0.2 已锁定，2026-11-18 用户拍板）

| # | 决定 | 定稿 | 理由 |
|---|---|---|---|
| 1 | 方案 A（langgraph）还是 B（自研状态机） | **A：LangGraph** | checkpoint 免费拿、图可视化、生态兼容；代价是多一串依赖 |
| 2 | checkpoint 用内存还是落盘 | **落盘（`data/agent_state/`，SqliteSaver）** | 复盘是 150s 级长任务，中断恢复是真实场景不是摆设 |
| 3 | coach 阶段独立节点，还是保持 prompt 隐式切换 | **独立节点+条件边** | 核心收益：研究证据不足时硬闸门拦下，prompt 叮嘱变代码约束 |
| 4 | 旧 chat_tools 删还是留 | **留活+对照注释**（用户拍板时的条件判定成立：chat_tools≈自研迷你编排框架） | ⚠️ 施工时发现 chat_tools 同时是**留言问答的活引擎**（server.py `_kb_qa_work`），不能注释死；方案=chat_tools 继续服务留言问答（单阶段简单循环），周复盘换图；代码内加 LangGraph 概念对照注释供学习 |
| 5 | 深度思考兜底（思考烧光预算重试）放节点内还是 llm 层 | **llm 层**（新增 `chat_step`，节点不感知模型细节） | 接入层职责 |

**边界补充（v0.2）**：LangSmith / LangGraph Platform 云服务不接（私有化原则，数据不出门）；
prebuilt create_react_agent 不用（两阶段业务结构是自定义图）；interrupt/流式/Subgraph
留到留言窗口主控台再启用。

## 6. 施工步骤（v0.2 对齐后展开）

1. requirements 加 `langgraph`，跑通最小 StateGraph 冒烟
2. 拆 chat_tools 循环体 → research / execute_tools / coach 三节点 + 条件边
3. checkpoint 落盘 + 中断恢复实测
4. 快照基线 + 对拍 + E2E
5. 文档 v1.0，公开库同步（只增不删）

## 变更记录

- v0.1（对齐期）：方向与边界初稿，§5 五项决定待拍板。
