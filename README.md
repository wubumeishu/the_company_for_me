# 🏢 the-company · 本地多智能体编排系统

> 把 Claude / Codex / 本地 Ollama 变成一支「虚拟软件公司」，用代码驱动 + 可视化节点的方式编排它们，替代死板的传统看板。
> 本地优先（LOCAL-FIRST）· UI 与引擎彻底解耦 · 运行全程透明可回退。

## 核心理念（对齐的三个开源项目）

| 参考项目 | 吸收的理念 | 在本项目中的落地 |
|---|---|---|
| [Haaziq386/Agent-Orchestration](https://github.com/Haaziq386/Agent-Orchestration) | React Flow 画布 + FastAPI/LangGraph 引擎，并行节点调度 | `frontend/` React Flow 渲染 DAG；`backend/` FastAPI 引擎按行并发调度 |
| [akudo7/kudosflow](https://github.com/akudo7/kudosflow) | 配置即 JSON，画布拖拽只写本地 `workflow.json` 来指挥后端 | 前端画布**唯一**写入口 = 覆盖 `workflow.json`，引擎只读它，UI 与引擎零耦合 |
| [gabriel-eidelman/agent-flow-visualization](https://github.com/gabriel-eidelman/agent-flow-visualization) | WebSocket 实时推送 Agent 思考 / 工具调用 / Shell 日志 | 引擎事件总线 → `/ws/run` → 前端 LiveLogPanel，彻底打破运行黑盒 |

## 三条严格约束

1. **同级节点同行（Same Row）**：React Flow 渲染时，同一 Level 的子任务节点严格锁在同一水平行，清晰展示并发逻辑。后端 Schema 以 `levels[]`（层级数组）支撑该布局，并强校验 `level.index == 数组下标`。
2. **透明化与持久化打卡**：所有 Agent 在执行实际代码/命令前，必须在项目根目录 `progress.md` 登记（时间 / 已完成 / 当前动作 / 下一步）。`progress.md` 是本地运行态，不入库。
3. **任务解耦 + QA/Review 节点**：节点出错时三件套全发 —— 控制台输出、流转图**红框高亮**、状态**定向回退**（`rollback.targetLevel`，带 checkpoint 的行内重跑，不是全量重启）。

## 架构

```
┌─────────────── frontend (React Flow 画布) ───────────────┐
│  FlowCanvas + RowConstraint(同级同行) + LiveLogPanel(Ws)  │
│  画布操作 → 仅覆盖 workflow.json（kudosflow 式，唯一写入口）│
└──────────────────────┬───────────────────────────────────┘
                       │ workflow.json (配置即 JSON)
┌──────────────────────▼───────────────────────────────────┐
│  backend (FastAPI 调度引擎)                                │
│  orchestrator: 行内 asyncio 并发 + gate(all/any) barrier  │
│  executor: ReAct 循环 · 工具白名单 · 角色边界强制           │
│  providers: Ollama(本地) / Codex CLI / Claude              │
│  events 总线 → /ws/run 实时流 · persistence: progress 打卡 │
└──────────────────────────────────────────────────────────┘
```

**三方解耦**：UI（React 组件）只管画布与日志展示；`workflow.json` 是唯一的「指挥信令」；引擎（FastAPI + 调度器）只读配置、管执行。任何一方都不侵入另一方。

## 核心数据契约

`workflow_schema.json`（JSON Schema draft-07，示例 `workflow.json` 已通过校验）：

- **`agents`** — 虚拟员工注册表：`role / provider / model / tools(白名单) / boundaries(mustNot + requiresHumanApproval)`。角色边界硬约束，杜绝任务越界。
- **`levels`** — 执行层级 = 画布行。`levels[i].nodes` 内并发执行，`gate: all|any` 决定行级 barrier；`index` 必须严格递增（同行不变量）。
- **节点** — `kind: agent | qa | review | gate`；`inputs` 用 `nodeId.outputName` 跨行接数据流；`onError.rollbackToLevel` 可覆盖全局回退目标。
- **`qa`** — 失败基线三件套 `console + redHighlight + rollback`，附 `retry{maxAttempts, backoffSeconds}`。
- **`providers`** — 本地 provider 端点注册（Ollama `http://127.0.0.1:11434`、Anthropic、Codex CLI）；key 只存环境变量名，不入 JSON。

## 目录结构（Phase 1 规划）

```
├── workflow_schema.json   # ★ 数据契约（draft-07）
├── workflow.json          # 示例工作流：L0 拆分 → L1 前后端并发 → L2 QA 评审
├── progress.md            # 打卡流水账（本地运行态，已 gitignore）
├── backend/
│   └── app/
│       ├── main.py        # ★ Python 调度引擎入口 (uvicorn app.main:app)
│       ├── schema/        # pydantic 镜像 + 同行不变量校验
│       ├── engine/        # orchestrator(行并发) · executor(ReAct) · rollback · events
│       ├── agents/        # ollama / codex / claude provider
│       ├── persistence/   # progress 打卡 + checkpoint
│       └── ws/stream.py   # ★ /ws/run WebSocket 实时流
└── frontend/
    └── src/
        └── components/
            └── canvas/FlowCanvas.tsx   # ★ React Flow 画布
                RowConstraint.tsx       # 同级节点同行约束 (Y 锁死 level)
                AgentNode.tsx           # 节点：工具徽章 + error 红框
```

## 运行方式

```bash
# 后端（backend/ 下，本机依赖已备：fastapi / uvicorn / jsonschema）
cd backend
python -m uvicorn app.main:app --port 8790

# 前端（frontend/ 下）
cd frontend
npm install && npm run dev          # http://127.0.0.1:5180

# 全链路：画布加载 workflow.json → 点 ▶ 启动 run（POST /run）
#          → 引擎按行并发执行 → /ws/run 事件流回画布（节点变色/红框）+ LiveLogPanel
```

## 验收命令（build 成功 ≠ QA 通过，必须实际运行）

```bash
# 后端 Phase 1 全链路 e2e（解析JSON→校验→并发执行→QA拦截→回退→打卡，15 项断言）
cd backend && python tests/test_e2e.py

# 后端 Phase 2 V2 单测（冷却恢复时间精确计算 + Tick 调度 + 敏捷小队 + 冷却挂起集成，30 项）
cd backend && python tests/test_resource_manager.py

# 后端 Phase 3a 单测（分支隔离 + PR 门禁 + 冲突红灯，17 项）
cd backend && python tests/test_git_policy.py

# 前端布局不变量（同级同行 Y 锁行 + X 均布，node 直跑）
cd frontend && node scripts/dump-snapshot.mjs

# 前端严格类型 + 生产构建
cd frontend && npm run build
```


## 版本控制规范

- **仓库**：`wubumeishu/the_company_for_me`（main 分支，push 走代理端口 7890）
- **SemVer + tag**：
  - `v0.x.y` — 契约/文档/骨架阶段（草案，允许破坏性修改）
    · `v0.1.0` 契约基线 / `v0.2.0` 文档基线 / `v0.3.0` Phase 1 骨架（mock provider 全链路跑通）
  - `v1.0.0` — 接真实 LLM provider（Ollama/Codex/Claude）后，引擎+画布+WS 三件套端到端可用
  - 每次里程碑打 tag：`git tag -a vX.Y.Z -m "..."` 并 push tag
- **分支**：`main` 始终可验收；功能开发用 `feat/<name>` 短分支，QA/Review 节点机制先行（红线：build 成功 ≠ QA 通过，必须实际运行验证）
- **入库规则**：契约/代码/文档入库；`progress.md`、`.env`、`node_modules/`、`__pycache__/`、`.venv/` 一律不入库

## 路线图

- [x] 数据契约 `workflow_schema.json` + 可校验示例
- [x] 仓库绑定 + 基线版本控制
- [x] Phase 1：FastAPI 引擎骨架（行并发 gather + barrier + QA 拦截回退）+ React Flow 同行布局 + WS 实时日志
      · 后端 e2e 15/15 PASS；前端 tsc 0 错 + 生产构建成功；布局不变量 node 实测
- [x] Phase 2a：后端资源调度 + 部门/小队架构（"机制先行，视觉延后"战略）
      · schema 升级：departments / tokenBudget / rate.windows / squads（向后兼容，旧配置零改动）
      · `resource_manager.py`：RateLedger 多窗口滑动冷却 + 状态机（ACTIVE/RESTING/SQUAD_DEPLOYED）+ TickDriver 心跳
      · `squad.py`：敏捷小队横抽 + SquadRoom 共享会议室 + QA 门禁 + 自动解散归建
      · orchestrator 重构：挂起等待（冷却→tick 唤醒→重跑）+ squad 节点分派
      · 单测 30/30 PASS（冷却恢复时间精确计算 / Tick 调度 / 小队 / 部门校验 / 冷却挂起集成）
- [ ] Phase 2b/2c：工位视觉（咖啡厅冷却皮肤、案卷堆叠高度映射 tokenBudget）、真实 LLM provider（Ollama/Codex/Claude）
- [x] Phase 3a：Git 分支隔离 + PR 门禁（三道防线）
      · `git_policy.py`：simulated 影子仓库 + 分支隔离（Dev 只在 `<branch>` 提交，引擎绝不直改主干）+ pr_gate 节点开 PR（冲突 dry-run + 门禁脚本，任一红灯=rejected 复用红框三件套）
      · schema 加 `git_policy` + Node.kind 枚举加 `pr_gate`（向后兼容）；validator 4e 关（pr_gate 必配 git_policy）
      · 第二道：`scripts/git-hooks/`（pre-push 拒直推 main + commit-msg 角色越界护栏）；第三道：`.github/workflows/ci.yml`（PR 自动跑 e2e+V2+3a 三套单测 + 前端 tsc/布局）
      · 单测 17/17 PASS（分支隔离/PR全绿/冲突红灯/门禁红灯/validator/全链路）；回归 45/45 全绿
- [ ] Phase 3b/3c：subprocess 真 git 后端 + 站会大盘（Git流/拦截Bug/燃尽/迟滞榜）+ 持久化数据基座（datastore 驱动）
