# ARCHITECTURE_V2 — 虚拟软件公司模拟器演进蓝图（大纲，未落码）

> 定位：基于 Phase 1 骨架（行调度引擎 + 同行画布 + WS 总线）的 **Phase 2/3 架构大纲**。
> 本文档是设计契约：先对齐机制与数据模型，评审通过后再动代码。
> 版本：v2-draft-1 · 2026-09-19 · 状态：待主人评审

---

## 0. 三阶段总览

| 阶段 | 主题 | 核心增量 | 对 Phase 1 骨架的改动面 |
|---|---|---|---|
| Phase 2 | 游戏化视觉 + 动态调度 | 工位负载映射、API 冷却=强制休息、敏捷小队（Tick 状态机） | 引擎加 tick 循环 + 资源账本；画布节点升级"工位"皮肤层 |
| Phase 3 | 防御性工程防线 | Git 分支隔离、PR/Lint 拦截、每日站会大盘 | 新增 QA 门禁子图 + git 策略层；全局看板新页面 |
| 持久化 | 统一数据基座 | 存储接口层，预留 BaaS 接入 | 顶层加 `datastore` 契约，本地 JSON 起步 |

铁律不变：UI 与引擎解耦，画布只通过 `workflow.json` 指挥引擎；所有机制状态都必须能进事件总线（WS 全透明）。

---

## 1. Phase 2：游戏化视觉与动态调度引擎

### 1.1 工位负载映射（Token 预估 → 桌面案卷高度）

**数据模型（schema 扩展，详见 §4）：**
- 节点级 `tokenBudget: { estimate: int, unit: "tok", contextHint?: int, outputHint?: int }`
  - `estimate` 由 **TokenEstimator** 在加载时计算（启发式：`task` 字数 + `inputs` 引用产物字节数/4 + `outputHint`），
    也可由 workflow 作者显式写死（写死优先）。
- Agent 级 `loadProfile: { concurrency?: int }` —— 该员工同时可扛的在办任务数（工位格子数）。

**前端渲染（NodeSkin 皮肤层，与逻辑节点解耦）：**
```
AgentNode.tsx 拆两层：
  └─ 逻辑层（现有）：provider 徽章 / 状态色 / 红框
  └─ 皮肤层（新增）：<DeskSkin load={estimate} status={nodeStatus} skin="default|casual|..."/>
       案卷堆叠高度 = clamp(estimate / 12000, 0.2, 1.0)   // ~42k tok → 0.85 满堆
       每 12k tok 渲染一格案卷（CSS 堆叠 div / SVG），冷却态整体变灰 + ☕ 角标
       皮肤可整体替换（后续接文生图生成的员工立绘，见敏捷小队 UI 段）
```
**渲染铁律**：负载量是**数据**（来自 tokenBudget，画布可写回 workflow.json），渲染只是映射函数 `loadOf(node)`，
绝不在组件里硬编码高度——保持 kudosflow 式"配置即数据"。

### 1.2 API 冷却即"强制休息"（rate ledger → 咖啡厅状态）

**数据模型：**
```jsonc
// providers[i] 扩展
{ "id": "claude-api", "type": "anthropic",
  "rate": { "windows": [ {"limit": 160, "periodSec": 60},
                         {"limit": 1500, "periodSec": 18000} ],
            "cooldownSec": 30 } }

// agents[x] 扩展
{ "provider": "claude", "stamina": { "max": 100, "regenPerMin": 10 } }
// （stamina = 公司资金/体力的游戏化外壳，窗口余量不足时强制进入 rest）
```

**引擎机制（新增 `engine/rate_ledger.py`）：**
```
RateLedger(provider_id):
    滑动窗口计数（deque + 周期重置点）
    consume(agent, task_tokens) -> bool      # executor 每次 LLM 调用前记账
    remaining_ratio(provider) -> float        # → WS 事件 rate_window
Tick 状态机（§1.3）每个 tick 检查：
    ratio 触底 或 真实 API 返回 429
      → AgentState = RESTING（强制休息）
      → 该 agent 名下所有排队任务挂起（不分配新任务）
      → 发 rate_limit_hit + rest_until(epoch) 事件
      → 周期重置点到达 → 自动回 ACTIVE（发 rest_over）
UI 呈现：工位皮肤切"咖啡厅冷却"态（☕ + 环形倒计时 = 窗口剩余秒），行调度跳过 RESTING 节点。
```
**持久化**：ledger 快照进 `.company/rate_ledger.json`（重启后余量不丢，冷却不绕过）。

### 1.3 敏捷矩阵动态组队（Tick/状态机 + 横向抽调）

**数据模型（schema 顶层新增 `departments` + `squads`）：**
```jsonc
"departments": [
  { "id": "pm", "title": "PM 部" },
  { "id": "ui", "title": "UI 部" },
  { "id": "dev", "title": "研发部" },
  { "id": "qa", "title": "QA 部" } ]

"agents": { "ui_designer": { "department": "ui", "tools": ["agnes-ai-image", "agnes-ai-video", ...], ... } }

"squads": {
  "squad_high_value_req": {
    "trigger": { "demand": "高价值需求标签" },        // 由需求节点标记触发
    "members": [ {"from": "pm",  "as": "pm"} ,
                  {"from": "ui",  "as": "ui", "toolsOverride": ["agnes-ai-image","agnes-ai-video"] },
                  {"from": "dev", "as": "dev"},
                  {"from": "qa",  "as": "qa"} ],
    "dissolveOn": "squad_qa_pass"                       // QA 通过 → 自动解散归建
  } }
```

**Tick 状态机（引擎新增 `engine/tick.py`，对齐 Python 游戏引擎节奏）：**
```
async def tick_loop():                    # 固定心跳（默认 500ms），全部状态流转在这里裁决
    for agent in company.agents:
        agent.state = transition(agent, now)   # 状态机：IDLE→ACTIVE→RESTING→IDLE / →SQUAD_DEPLOYED→IDLE
    dispatcher.assign()                   # 行调度只在 IDLE/ACTIVE 节点上派活
    squads.evaluate()                     # trigger 命中 → 横向抽调（冻结原部门编制）；dissolveOn 命中 → 解散归建
    await bus.publish("tick", states=..., utilization=...)   # → 前端大盘 + 工位色（每 N tick 才发，防刷屏）
```
- 状态机 = 纯函数 `transition(state, event)`，**可单测、可回放**（事件日志落 `.company/events.jsonl`）。
- 抽调语义：成员进入 SQUAD_DEPLOYED 后，原部门任务不再派给它；小队内子任务仍走 levels 行并发（嵌套子图）。
- 解散归建：小队 QA 节点通过 → 发 squad_dissolved 事件 → 成员回 IDLE，桌面案卷清空。

---

## 2. Phase 3：现代防御性工程防线

### 2.1 Git 分支隔离 + PR 拦截（含关键设计决策，见 §3）

**机制分层（三道防线，纵深防御）：**
```
第 1 道（引擎权威层）   Orchestrator：同一文件互斥锁 + Dev 节点强制 branch 后缀
                          （<node_id> 隔离分支），节点完成 = commit 到分支，禁止直推主干
第 2 道（本地 Git Hook） pre-push：拒绝无 PR 关联的 push；commit-msg：校验任务标签
第 3 道（远端 CI 门禁）  GitHub Actions：lint + 自动化测试 + 冲突检测 → 红/绿灯事件
                          全部结果回流事件总线 → QA Agent 消费
```

**PR 拦截链（新增 `engine/git_policy.py` + `agents/review_gate` 节点类型 `kind:"pr_gate"`）：**
```
Dev 分支完成
  → git_policy.open_pr(branch, base="main", check_scripts=[...])
  → 自动门禁并行跑：① 冲突检测（git merge-tree / dry-run merge）
                    ② 自动化测试脚本（仓库内 tests/，失败=红）
                    ③ 代码风格（ruff / eslint 等，违反 style guide=红）
  → QA Agent 消费门禁结果：
      任一红灯 → pr_rejected 事件 → 流转图红框（复用 Phase 1 三件套）→ 定向回退到 Dev 节点
      全绿 → pr_merged + git 证据（commit sha 链）写入产物 → QA 放行
铁律：QA 拦截必须带 git 证据（sha/branch），口头 PASS 不算数（对齐"build 成功≠QA 通过"红线）。
```

### 2.2 每日站会与数据大盘（全局面板）

**数据源（全部来自事件总线 + git，不在别处另造账本）：**
```
站会面板（新增前端 /standup 页）：
  ┌ Git 实时流：git 仓库 reflog/commit 事件（engine git_policy 每 commit 发 git_commit）
  ├ 拦截 Bug 数：按天聚合 pr_rejected / red_highlight 事件（含拦截原因 Top5）
  ├ 燃尽图：workflow levels 完成度 × 需求点估算（tokenBudget 加总）→ 剩余工作量曲线
  └ 员工迟滞榜：按 agent 聚合 (任务滞留时长 / RESTING 占比 / 被回退次数)
     → 管理者据此调资源排期（把任务改派：画布操作 → 写回 workflow.json，引擎下个 tick 生效）
"每日站会" = 引擎定时任务（cron 粒度）生成 standup 摘要事件 + 写 .company/standup_<date>.md
```

---

## 3. ★ 关键设计决策：Git 隔离用「Python 子进程模拟」还是「真实 Git Hook」？

**结论：分层混用 —— 引擎层一律用 Python 子进程（第一道防线 + 自动化门禁），真实 Git Hook 只挂"人肉操作"防线（第二道）。**

| 维度 | Python 子进程（`git` CLI） | 真实 Git Hook（pre-push/commit-msg） |
|---|---|---|
| 谁在调用 | 引擎 executor（程序行为，全自动） | 人手动 `git push` / `git commit`（绕过引擎的操作） |
| 可测试性 | 高：`subprocess.run(["git","branch",...])` 可 mock、可断言、可回放 | 低：hook 脚本分散在各仓库 `.git/hooks/`，CI 难统一验证 |
| 可移植性 | 引擎在哪跑 hook 就跟到哪；换远端仓库零改动 | 绑定到具体 checkout 的物理 `.git/hooks/`，clone 后易丢失 |
| 强制性 | 引擎权威层 100% 强（调度不经它 = 节点失败） | 可被 `--no-verify` / 删 hook 绕过 |
| 失败面 | 引擎自己控制错误语义（发事件、回退） | 黑盒，只能拿到 exit code + stderr |

**因此架构定稿：**
1. **引擎执行面 = Python 原生子进程**（`asyncio.create_subprocess_exec` + 工作目录/环境隔离）：
   分支创建、提交、merge dry-run、PR 开/合（走 gh CLI 或 REST），全在 `git_policy.py` 一个模块里，
   每个动作发 `git_action` 事件（可审计、可回放、可在前端大盘看见每个 Dev 的提交流）。
2. **仓库守卫面 = 真实 Git Hook（第二道，防人绕过）**：`scripts/git-hooks/` 提供安装脚本
   （`pre-push` 拒绝无 PR 关联的 push、`commit-msg` 校验任务标签），随仓库分发，`.git/hooks` 软链安装。
   它**不参与调度判定**——引擎永远以子进程结果为准，hook 只是"人类直接操作 git"的护栏。
3. **CI 门禁 = 第三道（可选远端）**：冲突/测试/lint 红灯以 webhook/轮询回流事件总线。
4. 模拟模式（Phase 2 过渡期）：`git_policy.backend = "simulated"` 用影子分支 + 内存 diff 引擎
   模拟 merge 冲突，不真动仓库——与 `mock` provider 同思路，保证 Phase 2 零外部依赖可跑全链路。

---

## 4. workflow_schema.json 扩展草案（1:1 可直接并入 v2 schema，向后兼容）

```jsonc
// ============ Agent 级扩展（原 Agent 定义 properties 内追加） ============
"department": { "type": "string" },                 // pm | ui | dev | qa | ops
"departmentRole": { "type": "string" },             // 部门内头衔（如 资深前端）
"stamina": { "type": "object",
             "properties": { "max": {"type":"integer"}, "regenPerMin": {"type":"integer"} } },
"loadProfile": { "type": "object",
                 "properties": { "concurrency": {"type":"integer","minimum":1} } },
"skin": { "type": "string" },                        // 工位皮肤 id（后续文生图立绘）

// ============ 节点级扩展（原 Node 定义 properties 内追加） ============
"tokenBudget": { "type": "object",
                 "properties": { "estimate": {"type":"integer"},
                                 "contextHint": {"type":"integer"},
                                 "outputHint": {"type":"integer"} } },   // 工位案卷高度数据源
"branch": { "type": "string" },                       // Dev 节点隔离分支（缺省 <node_id>）
"gate": { "type": "string", "enum": ["lint","test","style","conflict","all"], "default": "all" },
          // pr_gate 节点执行的门禁集（任一红灯 → 拦截打回）

// kind 枚举扩展（Node.kind）
// "agent" | "qa" | "review" | "gate"  →  + "pr_gate" | "squad" | "standup"

// ============ providers[i] 扩展 ============
"rate": { "type": "object",
          "properties": { "windows": { "type":"array",
                "items": { "properties": { "limit":{"type":"integer"}, "periodSec":{"type":"integer"} } } },
                         "cooldownSec": { "type":"integer" } } }   // → 强制休息/咖啡厅态

// ============ 顶层新增（与原 meta/agents/levels/qa/providers 平级） ============
"departments": { "type":"array", "items": { "properties": { "id":{"type":"string"}, "title":{"type":"string"} } } },
"squads": { "type":"object", "additionalProperties": { "properties": {
    "trigger": {...}, "members": { "type":"array", "items": { "properties": {
        "from":{"type":"string"},"as":{"type":"string"}, "toolsOverride":{"type":"array","items":{"type":"string"}} } } },
    "dissolveOn": {"type":"string"} } } },
"git_policy": { "type":"object", "properties": {
    "backend": { "type":"string", "enum": ["simulated","subprocess","hooks","ci"], "default": "simulated" },
    "baseBranch": {"type":"string","default":"main"},
    "checkScripts": {"type":"array","items":{"type":"string"} } } },
"datastore": { "type":"object", "properties": {
    "driver": { "type":"string", "enum": ["local-json","sqlite","baas"], "default": "local-json" },
    "collections": { "type":"array",
      "items": {"enum": ["hr_pool","growth","problem_bank"]} } } }
```

**不变量追加**：`squads[].members[].from` 引用的部门必须存在于 `departments`；
`tokenBudget.estimate` 若缺省，加载时由 TokenEstimator 补全（前端案卷高度才不至于空）。

---

## 5. 持久化数据基座（统一 API 接口层）

```
backend/app/datastore/
  store.py        ── 接口：StorageDriver（put/get/query/append），三实现：
                     LocalJson（.company/db/*.json，默认）/ Sqlite / BaaS（预留，接口先冻结）
  hr_pool.py      ── "HR 招聘池面板"：可招募 agent 模板（能力标签 + 价格档），squad 抽调的候选来源
  growth.py       ── 员工能力成长值：每完成节点 +Δ（按难度权重），决定后续可派任务上限
  problem_bank.py ── 公司踩坑知识库：不可篡改 = 每条记录存 (content_hash, prev_hash) 哈希链
                     （本地 JSON 也能做；BaaS 切换时只换存储，审计语义不变）
  api.py          ── FastAPI 路由 /datastore/<collection>（标准 REST，BaaS 接入时路由零改动）
```
铁律：**引擎核心（orchestrator/tick）只依赖 `StorageDriver` 接口**，不 import 任何具体存储——
BaaS 接入 = 新增一个驱动类 + 改 `datastore.driver` 配置，调度引擎一行不动。

---

## 6. 事件词汇表扩展（WS 总线新增事件，前端大盘/工位消费）

```
Phase 2:  rate_window(provider, remaining_pct, reset_in_s)
          rate_limit_hit(agent) / rest_over(agent) / rest_until(agent, epoch)
          tick(states, utilization) / squad_deployed(squad) / squad_dissolved(squad)
          load_change(node, estimate_tok)
Phase 3:  git_action(node, action, sha?) / git_commit(node, sha, msg)
          pr_opened(branch) / pr_rejected(check, reason) / pr_merged(sha)
          standup(date, digest_md)
```
（全部走现有 EventBus → Hub → /ws/run，LiveLogPanel 加配色即可，架构不变。）

## 7. 落地顺序（评审通过后按此排期）

1. **Phase 2a**：schema 扩展 + TokenEstimator + DeskSkin 案卷渲染（负载可视化先跑通）
2. **Phase 2b**：RateLedger + tick 状态机 + 咖啡厅冷却态（用真实窗口参数 160/60s、1500/5h 做夹具测试）
3. **Phase 2c**：squads 抽调/解散（ui 成员接文生图/视频工具 = toolsOverride 已就位）
4. **Phase 3a**：git_policy（simulated 后端）+ pr_gate 节点 + 拦截三件套复用
5. **Phase 3b**：subprocess 后端 + 仓库分发 git hooks + CI 红灯回流
6. **Phase 3c**：站会大盘（Git 流 / 拦截 Bug / 燃尽 / 迟滞榜）+ datastore 接口层
7. **持久化**：datastore 三驱动 + problem_bank 哈希链（BaaS 驱动仅留接口桩）

---

### 待主人拍板的开放问题

1. tick 心跳默认 500ms，前端大盘要不要独立低频通道（1s 聚合一次）防 WS 刷屏？
2. 燃尽图的"需求点"用 tokenBudget 估算量还是显式 `storyPoints` 字段（更准但配置更重）？
3. `problem_bank` 哈希链要不要接 git 提交做"存证"（每追加一条自动 commit 到 main）？
