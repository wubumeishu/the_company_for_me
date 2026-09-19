# PHASE_3_ENGINEERING_DEFENSE — 第三阶段工程防线架构书

> 定位：在 Phase 3a（simulated 影子仓权威层已合入 v0.5.0）之上，落地 **subprocess 真 git + 强制结构化交接 + LangGraph 式回溯纠错** 的完整工程防线。
> 方法论：**博采众长，降维打击** —— 吸收 MetaGPT / ChatDev / CrewAI / AutoGen / LangGraph 的架构精髓，落到本仓库既有骨架（行调度 + 三件套 + CheckpointStore + EventBus）上，不引入新框架，只"取其魂、固其形"。
> 版本：v3-defense-2 · 2026-09-20 · 状态：**主人评审通过（5 项拍板全确认，见 §7 拍板记录），新增两大吸收项（§1.4 Stack-Roadmap / §1.5 Agency-Agents），3b-① 地基已动工**

---

## 0. 战略纲领：博采众长，降维打击

我们不照搬任何单一框架，而是把每个框架"最值钱的那一块"抽出来，缝进我们的纵向切片里。取舍原则：

1. **LOCAL-FIRST 不动摇** —— 能用 git worktree 达成"崩溃不污染主干"，就不上 Docker 硬隔离；Docker 只留给"执行不可信测试脚本"这一个环节。
2. **单一权威写入口**（kudosflow 铁律延伸）—— 引擎是**唯一** git 写入口；每个 Dev 的 LLM 只产"交接文档"，git 动作一律由引擎子进程代劳。
3. **全事件可回放** —— 每个 git 动作 / 每道门禁 / 每次回滚都进 EventBus（WS 全透明），前端大盘与 LangGraph 式"时旅"都靠这份事件日志。

| 框架 | 最值得偷的一块 | 我们的取舍 | 落地位置 |
|---|---|---|---|
| **MetaGPT** | "Code = SOP(Team)"、角色间**强制结构化交接**（PRD/设计/任务卡） | 偷"SOP + 交接契约"，不偷其重型 role 图 | `dev_handoff` 交接文档 + PR 描述契约（§1.1） |
| **ChatDev** | 微分阶段 + 每阶段产出**机器可读工件**（API 文档/测试计划）+ 交叉评审 | 偷"工件即契约"：QA 校验的是**文档清单**而非"看代码" | `reviewCriteria` → 结构化 checklist（§1.1） |
| **CrewAI** | 任务输出→输入 piping（`outputs[]` 链）+ process control | 我们已有 `outputs[]` 跨行数据流，补一道**产物 schema 校验** | executor 出口加 artifact 校验（§1.1） |
| **AutoGen** | **沙盒化代码执行**（Docker/命令执行器）+ 群聊 speaker 选择 | 偷"执行面沙盒"，但主隔离用 **git worktree**（更贴合 git 分支目标） | `run_subprocess` 原语 + Docker/venv 可选执行层（§1.2/§2.1） |
| **LangGraph** | **检查点 + 条件边 + time-travel 回滚**（super-step 级） | 偷"状态机图 + 断点回溯"，缝合进既有 CheckpointStore | `onError` 条件边 + 有界自动纠错 + 优雅回滚（§1.3/§2.3） |
| **Stack-Roadmap**（easychen） | **路径依赖可视 + 进度解锁**（前序没过则下游整条锁定） | 偷"解锁"，但把展示层视觉逻辑升级为**引擎前置强依赖判定** | 调度前强依赖检查 + QA 红灯时 Blocked 态 BFS 传播下游（§1.4） |
| **Agency-Agents**（msitarzewski） | **标准化真实中介调用 + 中介授权层（副作用分级 / human-in-the-loop）** | 偷"中介授权闸门"，缝到既有工具白名单之上 | `tool_authorize` 层③副作用分级 + external 调用物理拒绝/预留审批断点（§1.5） |

---

## 1. 吸收思路与落地映射

### 1.1 MetaGPT/ChatDev 的 SOP 流水线：强制"标准化交接文档"

**痛点**：Agent 之间不能只是"传话"（LLM 说"我写好了"→ QA 盲信）。MetaGPT 的核心洞见是 **每个角色输出强制的结构化文档**（PRD / 设计 / 任务卡），下游角色消费的是**文档契约**，不是自由文本。

**我们的落地（SOP 交接链）：**

```
Dev(LLM)  ──►  强制交接文档 dev_handoff（§4 契约）
                ├─ commit_message   规范化（node_id 前缀 + Conventional Commits）
                ├─ changed_files[]  产物 → 目标文件映射
                ├─ self_check{}     Dev 自查声明（我跑了什么 / 结果）
                └─ pr_description   给 QA 的 PR 说明（含验收要点）
                    │
                    ▼
              [SOP Gate] 引擎校验 dev_handoff schema
                    ├─ 缺字段/格式错 → 节点 FAIL（不是"口头完成"）→ 三件套
                    └─ 通过 → 引擎代劳 git commit（§2.2）
                    │
                    ▼
              QA/review 节点：消费 pr_description + reviewCriteria(checklist)
                    └─ 逐项核销，红灯必须指到"哪条 checklist + 哪个文件"（可溯源）
```

**降维打击点**：MetaGPT 的 SOP 是"整条流水线的角色图"，我们**只取"交接即契约"这一环**，把它做成 `dev_handoff` 结构化产物 + 一道 SOP Gate。这样 Dev "写完代码" 与 "提交 git" 之间有了**不可跳过的文档闸门** —— 呼应主人红线"build 成功 ≠ QA 通过，口头 PASS 不算数"。

### 1.2 AutoGen 的沙盒安全执行：让崩溃永远碰不到主干

**痛点**：绝不能让 Agent 在真实系统里乱执行代码。AutoGen 的答案是**沙盒化命令执行**（`DockerCommandLineCodeExecutor`），代码跑在隔离容器里，挂了不影响宿主。

**我们的落地（分层隔离，git worktree 为主）：**

```
┌ 层 ① 进程隔离（必做）  所有 AI 子进程走 run_subprocess 原语
│    asyncio.create_subprocess_exec(cwd=sandbox, env=最小化, timeout)
│    + start_new_session / 进程组 kill（挂起的子进程随父死，不泄漏）
│
├ 层 ② Git 状态隔离（主隔离原语）  ← 真正保证"崩溃不污染主干"的
│    .company/git-sandbox/  引擎私有影子仓（绝不碰主人真实 .git）
│    ├─ 每个 Dev 节点 → git worktree add worktrees/<node_id> b/<node_id>
│    │      （工作树物理隔离：A 改坏了自己的 worktree，B 与 main 毫发无伤）
│    └─ main head 唯一合法推进路径 = pr_gate 全绿后 git merge --squash
│       其它任何写 main 的调用，引擎层直接拒绝（单写入口）
│
└ 层 ③ 运行时隔离（可选，AutoGen 式）  仅 checkScripts 执行真实测试时
     默认：venv + 独立 cwd（LOCAL-FIRST，零外部依赖）
     升级：backend=ci 时走 Docker 容器跑测试（不可信脚本才进容器）
```

**为什么用 git worktree 而不是"直接开 N 个 clone 分支"**：worktree 共享同一个对象库（省空间、秒开），但**每个 worktree 的 HEAD 与工作区独立**——正是"每个员工一张独立工位，工位炸了办公室不倒"的物理模型，且天然契合"员工只能在各自 branch 写代码"。

### 1.3 LangGraph 的状态持久与回溯：QA 打回如何优雅回滚

**痛点**：QA 发现致命冲突打回任务，如何**优雅回滚该节点状态**而非整条重跑？LangGraph 的答案是 **checkpointer（每个 super-step 存断点）+ 条件边 + time-travel（跳回任意断点重放）**。

**我们的落地（缝合进既有 CheckpointStore + orchestrator）：**

```
LangGraph 概念              →   我们既有物                       →   Phase 3 增强
──────────────────────────────────────────────────────────────────────────────
StateGraph                →   orchestrator 的 levels[] 行图
checkpoint (per super-step)→   CheckpointStore（按 level 存已通过产物）
条件边 conditional edge   →   node.onError.policy（failFast/retry/skip）
time-travel(跳回重放)      →   rollback_to_target_level（定向重跑目标行）
interrupt/resume          →   RESTING 挂起（限流精确时刻唤醒，2c 已有）
  + 本轮新增：红色 flag 作为"回溯触发器"，注入 Dev 下一回合自纠错
```

**关键洞察（LangGraph "jump to checkpoint"）**：QA 红灯**不是失败终点，而是一次"条件边跳转"**——跳回 Dev 节点的 super-step，把 `red_flags`（哪条测试挂了 + 报错段）作为上下文注入 Dev 的 ReAct 下一回合，让它**自纠错**。断点保留已通过的上游行（checkpoint），只重放被打回的那一行。详见 §2.3。

### 1.4 Stack-Roadmap 的"大盘路径依赖可视 + 进度解锁"：让后向节点在 UI 上"看得见受阻"

**理念注入（easychen/stack-roadmap）**：Stack-Roadmap 不是"画一堆节点"，而是**路径依赖图 + 进度解锁**——一条 roadmap 上每个里程碑有明确的**前置依赖链**，前面没过，后面整条灰掉/锁定，用户一眼看到"我卡在哪一步、解锁后下一步才亮"。我们要把这个机制从"给人看的静态图"升级成"**给引擎判定的活状态机**"。

**我们的落地（前置强依赖 + Blocked 态传播）：**

```
Stack-Roadmap 概念        →   我们既有物                     →   Phase 3 增强
──────────────────────────────────────────────────────────────────────
路径依赖图                →   levels[] 行图（inputs 跨行引用）
进度解锁（里程碑点亮）     →   节点/行 pass 后下游才可调度
"前面卡住→后面全灰"      →   ★ 新增：Block 态传播（本条核心）
```

**核心规则（引擎权威层强制，不是 UI 装饰）：**

```
① 前置强依赖检查（调度前）
   orchestrator 调一行 node 之前，先查该行所有前置 input 引用的
   上游节点是否 ok。任一上游 = FAIL/BLOCKED/未调度 → 本行
   直接置为 BLOCKED（不调度、不发 node_start，避免"上游还没
   影子的下游瞎跑"）。这是 Stack-Roadmap"解锁"的引擎化：
   没解锁 = 物理上不进调度，不是 UI 上灰着假装没跑。

② 红灯阻塞传播（QA 拦截时，本条新增的架构调整）
   PR 被 QA 拦截（pr_rejected，红灯）→ 不只打回 Dev，还要把
   【该 pr_gate 节点 + 所有它的下游依赖节点（BFS 遍历 levels
   的 inputs 反向图）】标为 BLOCKED 态，大盘上显示"受阻"
   描边（区别于"失败"红框——失败是已跑挂，受阻是压根没资格跑）。
   数据模型：NodeOutcome 增 blocked_reason ∈ {upstream_red,
   upstream_missing, rate_wait, squad_deployed}，前端 AgentNode
   按 reason 着色（受阻=琥珀锁链描边，失败=红框）。
   事件词汇新增：node_blocked(node, reason, blocked_by[])（供大盘
   渲染"该节点 + 下游 N 个节点被红灯连锁锁住"）。

③ 解锁恢复（纠错/回退全绿后）
   Dev 自纠错重跑 → pr 转绿 merged → 引擎重新 BFS 下游，把
   原 BLOCKED 节点逐个翻回 READY 并继续调度。
   不变量：BLOCKED 是"派生态"（由上游 pr 红/绿 + 上游 ok 算出），
   不是独立存储位——上游一转绿，下游自动解锁，无需人工清。
```

**为什么是"引擎强依赖"而非"UI 画灰"（降维打击点）**：Stack-Roadmap 的解锁是**展示层**的视觉逻辑，我们的升级是让这条依赖图**可被引擎消费**——调度器真的会拒绝调度未解锁的下游，QA 红灯真的会物理锁死下游。UI 的"受阻"只是引擎 Blocked 态的投影，**权威在引擎，不丢给前端判断**。

### 1.5 Agency-Agents 的"标准化真实中介调用 + 中介授权层（Middleware Auth Layer）"

**理念注入（msitarzewski/agency-agents）**：agency-agents 的核心不是"更多 agent"，而是**每一个带副作用的真实外部调用（发邮件、推远程仓、调第三方 API）都要过一层标准化的中介授权（middleware auth）**——agent 声明它要调谁、agent 的白名单里有没有这个工具、这个调用是不是"写真实世界"（副作用），三者都过才放行；未来还能在高风险副作用上**挂人类审批断点（human-in-the-loop approval breakpoint）**。我们要把这个"中介授权层"做成我们工具白名单之上的**强制闸门**，让 AI 员工不只是在沙盒里自娱自乐。

**我们的落地（工具白名单 → 中介授权层，分层）：**

```
┌ Agent 想执行一次工具调用 tool_call(name, args)
│
├ [层 ① 白名单检查]  agent.boundaries.tools / agent.tools 白名单
│    该工具在不在"被允许名单"？不在 → 拒绝（现有 2b 逻辑，不变）
│
├ [层 ② 副作用分级]（★ 本条新增，Agency-Agents 中介授权）
│    给每个工具打"副作用标签" side_effect ∈ {
│        none            —— 纯读/纯本地脚本，零真实世界影响（沙盒自娱）
│        local_write     —— 写本地文件/worktree（影子仓内，引擎单写入口管）
│        external_side   —— 带真实外部副作用（发邮件/推远程/调 3rd API）
│    }
│    · none      → 放行（不经中介层，快路径）
│    · local_write → 引擎代劳（Dev 不直接写，走 dev_submit 单写入口，§2.2）
│    · external_side → ★ 必须过 [层 ③]，否则物理拒绝
│
└ [层 ③ 中介授权闸门 Middleware Auth Layer]（★ 核心，本条落地）
     authorize(agent, tool, args) -> Verdict {
        allowed: bool
        reason: str                    # 拒绝原因（可溯源进事件流）
        approval: Optional[str]        # 若需人工审批：审批断点 token
     }
     授权判定顺序（AND 全过才放行）：
        1) 工具在 agent 白名单（层①重校，防白名单被绕过）
        2) 工具 side_effect != external_side，或
           （external_side 且 agent 被显式授予该外部权限 grant）
        3) 【未来预留】该 external 调用是否要求人类审批断点：
             若命中高风险规则（如 push 到 owner 远端仓 / 发外部邮件）
             → 返回 approval=breakpoint_token，挂起等 human-in-the-loop
             （Phase 3 先留接口 + 默认"拒绝高风险外部写"，审批断点后续接 WS 弹窗）
     放行/拒绝都发事件：tool_authorize(node, tool, side_effect,
        verdict) —— 中介层全动作可回放（与 git_* 事件同级别透明）
```

**不变量（Agency-Agents 铁律，写进引擎权威层）**：
- **沙盒默认安全**：`side_effect=none/local_write` 的工具永远不直接碰真实世界；
  所有"写主干/推远端/发外部"一律**引擎代劳 + 过层③**。
- **拒绝优于静默**：external 调用无 grant → **物理拒绝并发 tool_authorize 红事件**，
  绝不"假装执行成功"（呼应主人红线 build 成功≠QA 过）。
- **审批断点是可选增强**：默认 `external_side` 直接拒绝（最安全档）；
  未来接 human-in-the-loop 时才放行到"挂起等审批"，接口已就位（approval 字段），
  不改变当前"LOCAL-FIRST 不碰远端"语义。

**降维打击点**：Agency-Agents 的中介授权是它区别于"一堆 agent 各自乱调 API"的**灵魂**。我们只取"副作用分级 + 强制中介闸门 + 可回放授权事件 + 预留审批断点"这一环，缝到既有工具白名单之上——agent 从"沙盒里自娱自乐"升级到"想碰真实世界必须先过标准化授权层"，且全程事件透明。

---

## 2. 三问定解（主人点名，先给结论再给论证）

### 2.1 Q1 · 用什么技术隔离 AI 员工工作区？（Subprocess vs 虚拟环境）

**结论：不是二选一，是三层叠加，且"主隔离原语"是 git worktree，不是 venv，也不是 Docker。**

| 隔离目标 | 用什么 | 为什么 |
|---|---|---|
| 子进程乱跑命令不炸宿主 | `run_subprocess`（asyncio + 进程组 kill + timeout） | AutoGen 式执行面沙盒，**必做** |
| 员工崩溃不污染主干 | **git worktree 影子仓**（层②） | git 状态层隔离，**这是真正的"各写各的 branch"物理保证** |
| 员工互不串改 | 每个 worktree 独立 HEAD + 引擎单写入口 | A 的 worktree 炸了，main 与 B 毫发无伤 |
| 执行不可信测试脚本 | 默认 venv；`backend=ci` 时 Docker | 只有"跑真测试"才需要运行时隔离（LOCAL-FIRST 按需升级） |

**一句话**：venv 只隔离"Python 跑起来用什么依赖"，**隔离不了"git 写到了哪条 branch"**；真正守住"员工只能在各自分支写"的是 **git worktree + 引擎单写入口**这一层。Docker 是最后一道（不可信脚本），不是默认——这符合主人的 LOCAL-FIRST 偏好。

### 2.2 Q2 · AI 触发 git commit / PR 的后端执行逻辑与数据流向

**铁律重申：Dev(LLM) 从不直接 `git commit`。它只产 `dev_handoff`，git 动作全由引擎子进程代劳**（单一权威写入口 + MetaGPT SOP 闸门）。

```
[Dev 节点 LLM 执行]
   executor._run_llm → 真实模型返回代码 + dev_handoff（结构化交接文档）
        │  SOP Gate：校验 dev_handoff schema（缺字段/格式错 → node FAIL → 三件套）
        ▼
[引擎 dev_submit(node, handoff)]            ← git_policy.py（subprocess 实现）
   run_subprocess("git -C worktrees/<node> add/commit", ...)   发 git_commit 事件
        │
        ▼
[pr_gate 节点 open_pr]
   ① 冲突 dry-run：git merge-tree base...b/<node>  （或 git merge --no-commit --no-ff）
   ② 门禁脚本 checkScripts[]（test/lint）→ 逐条 run_subprocess，捕获 returncode + 日志
   ③ 组装 PR（gh CLI 或 push+REST）：pr_description = dev_handoff
        │  每条 check 发 pr_checks 事件（name/passed/detail，前端红框定位到"哪条门禁"）
        ▼
[gate 判定]
   全绿 → git merge --squash 推进 main → pr_merged 事件（带 merge sha，QA 可溯源）
   任一红灯 → pr_rejected + red_flags → 进入 §2.3 自动纠错
```

**数据流向（每步都进事件总线，WS 全透明，是大盘数据源）：**
`dev_handoff`（结构化）→ `git_commit`（sha/branch）→ `pr_opened`（branch→main）→ `pr_checks[]`（逐条门禁红/绿）→ `pr_merged | pr_rejected`。**LLM 产文档、引擎跑 git、门禁出红绿、事件全透明**——四层职责分明。

### 2.3 Q3 · QA 拦截节点亮红灯时，自动纠错重试机制怎么跑？

**核心：红灯 = 一次 LangGraph "条件边跳转 + 断点回滚"，不是失败终点。有界、可溯源、不整条重跑。**

```
pr_rejected（red_flags: [{check:"test", file:"x.py", detail:"AssertionError..."}]）
        │
        ▼  [条件边：node.onError.policy]
   ┌─────────────────────────────────────────────┐
   │ policy=retry（自动纠错，默认）                 │
   │   ① 冻结断点：CheckpointStore 保留已通过的上游行产物
   │   ② 注入纠错上下文：把 red_flags（哪条挂+报错段）写进
   │      目标 Dev 节点下一回合的 ReAct 上下文（prev_outcomes + error）
   │   ③ Dev 自纠错重跑（真实 LLM 读到"哪里挂"→ 改）→ 重新 dev_submit
   │   ④ 重新走 pr_gate；仍红 → 回 ①，计数 +1
   │   ⑤ 达 qa.retry.maxAttempts 仍红 → 触发 policy=rollback
   ├─────────────────────────────────────────────┤
   │ policy=rollback（优雅回滚，LangGraph time-travel）│
   │   定向回退到 qa.rollback.targetLevel（Dev 行），只重放该行
   │   上游已过行的产物从 checkpoint 注入（不会重算）
   │   Dev 分支不销毁：纠错后的 commit 叠在原 branch 之上（main 始终未动）
   ├─────────────────────────────────────────────┤
   │ policy=failFast（红线兜底）                    │
   │   立即红灯收口 → 三件套（console+红框+回退）→ run FAILED
   └─────────────────────────────────────────────┘
        │
        不变量：main head 只在"全绿 + squash"时推进；
                任何红灯路径都不碰 main（"没过 QA 绝不合主干"由 git 层物理保证）
```

**优雅回滚的关键（LangGraph "jump to checkpoint" 语义）**：
- 断点 = 该 level 行 **通过时** 的 `CheckpointStore` 快照（已通过节点产物 + 分支 head）。
- 回滚 = 从 `targetLevel` 重放，`prev_outcomes` 用断点产物喂 inputs（复用既有"回退重跑注入上游产物"坑位经验，避免上游读成 missing）。
- **Dev 分支保留**：纠错是在同一 branch 上叠新 commit，不是推倒重开——主干从未被污染，历史可 `git log` 全程溯源。

---

## 3. 三道防线（对齐 ARCHITECTURE_V2 §3，subprocess 落地）

```
第 1 道 · 引擎权威层（subprocess，本轮主战场）
   git_policy.py: backend="subprocess" → SubprocessGitPolicy（对标现有 simulated 接口不变）
   - run_subprocess 原语（进程隔离 + 流式日志 + returncode + 超时）
   - SandboxRepo：影子仓 init + worktree add/prune + 隔离 env
   - dev_submit / open_pr 真 git 实现；checkScripts 真跑（venv/Docker 可选）
   强制度：调度不经它 = 节点失败（引擎 100% 权威）

第 2 道 · 仓库守卫（真实 git hooks，防"人绕过引擎"）
   scripts/git-hooks/{pre-push,commit-msg} + install.sh（已就位）
   - pre-push：拒绝无 PR 关联的 push；commit-msg：校验 node_id 前缀
   强制度：hook 只拦"人类手操作 git"，不参与调度判定（引擎以子进程结果为准）

第 3 道 · 远端 CI 门禁（GitHub Actions，可选）
   .github/workflows/ci.yml（已就位）：PR 自动跑 checkScripts + 不变量
   红灯经 webhook/轮询回流事件总线 → QA 消费
```
**纵深逻辑**：三道各挡一类威胁——引擎挡"程序自己乱写"，hooks 挡"人绕过引擎"，CI 挡"远端合入前最后一道"。subprocess 是引擎层的"从 simulated 影子仓 到 真 git" 的升级，**接口对 executor/pr_gate 完全透明**（backend 切换只换 impl）。

---

## 4. 数据模型与契约扩展（schema 草案，可 1:1 并入 v2 schema，向后兼容）

```jsonc
// ============ 新增：Dev 交接文档（MetaGPT SOP 契约）============
// 作为 node 的 outputs 之一：Dev 节点必产出 name="dev_handoff", kind="artifact"
"dev_handoff": {
  "commit_message": "n1_fe: 实现画布同行约束布局",   // 强制 node_id 前缀 + Conventional
  "changed_files": ["frontend/src/..."],
  "self_check": { "ran": ["npm run build"], "passed": true },
  "pr_description": "改动点/验收要点/风险（QA checklist 依据）" }

// ============ pr_gate 节点扩展（§2.2 数据流）============
// checkScripts[] 已有；本轮补：
"git_policy": {
  "backend": "subprocess",          // 现切到 subprocess（simulated 保留为过渡）
  "baseBranch": "main",
  "repoPath": ".company/git-sandbox",   // ★ 影子仓，绝不动主人真实 .git（LOCAL-FIRST）
  "worktreeRoot": ".company/git-sandbox/worktrees",
  "checkScripts": ["python -m pytest backend/tests/", "node scripts/dump-snapshot.mjs"],
  "execSandbox": "venv|docker",    // ★ 新增：checkScripts 的运行时隔离层（默认 venv）
  "checkTimeoutSec": 300 }

// ============ 事件词汇表扩展（WS 总线新增，前端大盘/git 实时流消费）============
// 既有：git_commit / pr_opened / pr_checks / pr_merged / pr_rejected
// 新增：git_log(node, stream, line)   ← 子进程流式日志（复用 shell_log 词汇，前端零改动）
//       git_sandbox_init(root)        ← 影子仓/worktree 初始化
//       rollback_jump(from, to, red_flags) ← LangGraph 式条件边跳转（回退可溯源）
//       auto_fix_attempt(node, attempt, red_flags) ← 自动纠错计数（防死循环可视化）
// ★ §1.4 Stack-Roadmap 路径依赖可视（进度解锁引擎化）：
//       node_blocked(node, reason, blocked_by[])   ← 红灯连锁锁下游（大盘琥珀锁链描边）
//       node_unblocked(node)                        ← 上游转绿，下游解锁恢复调度
//       NodeOutcome.blocked_reason ∈ {upstream_red, upstream_missing, rate_wait, squad_deployed}
// ★ §1.5 Agency-Agents 中介授权层：
//       tool_authorize(node, tool, side_effect, allowed, reason, approval?) ← 授权层全动作可回放
//       tool_side_effects 白名单标签：none / local_write / external_side（配在 agent.boundaries）
```

---

## 5. 关键数据流全景图（subprocess + 自动纠错）

```
   Dev LLM ──dev_handoff──► [SOP Gate 校验] ──► 引擎 dev_submit
                                              │ run_subprocess(git worktree add/commit)
                                              │   发 git_commit / git_log
                                              ▼
                                   b/<node> 分支（独立 worktree）
                                              │
                     pr_gate: merge-tree 冲突检测 + checkScripts(venv/docker) 真跑
                                              │ 发 pr_checks[]
                          ┌───────────────────┴───────────────────┐
                        全绿                                    任一红灯
                          │ git merge --squash → main            │
                          │ 发 pr_merged(sha)                    ▼
                          │                              rollback_jump（条件边）
                          │            ┌───────────────────┴──────────────────┐
                          │        retry: Dev 注入 red_flags 自纠错重跑(≤maxAttempts)
                          │        rollback: 定向回退 targetLevel，checkpoint 喂上游，branch 保留
                          │        failFast: 三件套收口
                          │                       │
                          └───────────┬───────────┘（所有红灯路径 main 均未动 = 物理保证）
                                       ▼
                              全绿才 squash 进 main（QA 可溯源 sha）
```

---

## 6. 落地顺序（3b → 3c，评审通过后按此排期）

1. **3b-①** ✅ **已完工**（2026-09-20）`run_subprocess` 原语 + `SandboxRepo`（影子仓 init / worktree add-prune / 隔离 env / venv 默认档 / merge_squash / merge_dry_run）——`backend/app/engine/subprocess_sandbox.py`，26 项验收全绿（离线 FakeRunner 零真进程 + tmp 影子仓真 git + 真 venv 双链；红线"红灯路径 main 全程未动"实测通过）。
2. **3b-②** `SubprocessGitPolicy`：`dev_submit`/`open_pr` 真 git 实现 + 分支路由；`git_policy.py` 一处接线（`backend=="subprocess"` 时切 impl，接口不变，底层换 `SandboxRepo`）。
3. **3b-③** `dev_handoff` SOP 契约 + SOP Gate 校验（强契约，拍板 #4）；checkScripts 真跑（venv 默认 `SandboxRepo.run_in_venv`，Docker 可选 `execSandbox`）。
4. **3b-④** 自动纠错回路：`red_flags` 注入 + `retry`/`rollback` 条件边 + **maxAttempts=3**（拍板 #3）+ 分支保留（LangGraph time-travel 语义）；**§1.4 node_blocked 红灯连锁传播** + **§1.5 tool_authorize 中介授权层** 随本步接入。
5. **3c** 站会大盘（git 实时流 / 拦截 Bug / 燃尽 / 迟滞榜，**受阻=琥珀锁链描边**视觉档）+ datastore 持久化基座（哈希链 problem_bank）。

**测试铁律（主人红线：build 绿 ≠ QA 过）**：
- 离线可测：注入 fake `run_subprocess` → 分支路由/门禁红绿/回滚断言全程**不发真进程**（同 2c transport 注入思路）。
- 真跑链路：tmp 影子仓真起 `git`，跑 Dev→PR→门禁红/绿两条链，断言 **main 全程未被红灯路径污染**。
- 回归：3a 的 17 项 simulated 测试 + 全引擎 e2e 保持全绿（backend 切换零回归）。

---

## 7. 拍板记录（主人 2026-09-20 全部确认 ★）

| # | 议题 | 主人拍板 | 落地形态 |
|---|---|---|---|
| 1 | 影子仓位置 | **选项 A：`.company/git-sandbox/` 每 Dev 独立 worktree 物理隔离** | `SandboxRepo(root=.company/git-sandbox)`，`git worktree add worktrees/<node_id> b/<node_id>`；每 run 可 fresh 重建，**绝不碰主人真实 `.git`**（LOCAL-FIRST 红线） |
| 2 | execSandbox 默认档 | **venv 默认隔离 + Docker 可选** | checkScripts 默认 `venv`（零外部依赖）；`execSandbox:"docker"` 显式开启才进容器（不可信测试脚本专用） |
| 3 | 自动纠错上限 | **maxAttempts = 3** | `qa.retry.maxAttempts: 3`（2 太小纠错空间不足，3 有界且防 token 无限烧） |
| 4 | dev_handoff 强度 | **强契约（缺字段 = SOP Gate 直接 FAIL）** | MetaGPT 强制文档铁律：交接文档缺一字段即节点红，呼应"build 成功 ≠ QA 过"红线 |
| 5 | PR 落地形态 | **本地 squash merge 影子仓（纯 LOCAL-FIRST）** | `open_pr` 全绿 → `git merge --squash` 进影子仓 main；PR 只在事件流体现，不碰远端 GitHub（远端 CI 留作可选第三道） |

> 以上 5 项 + §1.4 Stack-Roadmap（大盘路径依赖可视 / 前置强依赖 Blocked 传播）+ §1.5 Agency-Agents（中介授权层 / 副作用分级）已全数武装完毕 → **3b-① 开工**（`run_subprocess` 原语 + `SandboxRepo` 影子仓地基，选项 A + venv 隔离，不改任何现有视觉布局逻辑）。
