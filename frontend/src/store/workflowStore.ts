/**
 * workflowStore —— 画布状态 ↔ workflow.json 双向（kudosflow 式：保存=仅覆盖文件）。
 *
 * 状态分三层：
 *   1) workflow：解析自本地 JSON 的图数据（nodes 位置由 levelLayout 算，不是自由拖）
 *   2) nodeStatus：运行态（idle/running/ok/error）—— 由 /ws/run 事件驱动
 *   3) logEvents：WS 事件时间线（LiveLogPanel 消费）
 */
import { create } from "zustand";
import type { NodeDef, Workflow, BusEvent, NodeStatus } from "../lib/types";
import { layoutAll, type LevelLayout } from "../lib/levelLayout";

interface RunSnapshot {
  /** 由 workflow.json 计算出的行列布局（画布渲染输入） */
  layouts: LevelLayout[];
  workflow: Workflow;
  nodeIdToLevel: Record<string, number>;
}

/** Phase 2c：工位负载（案卷高度数据源）= load_change 事件的实时 estimate */
interface NodeLoad {
  estimate: number;              // token 预估量（declared/estimated 双源）
  source: "declared" | "estimated";
  actual?: number;              // 真实 LLM 返回的 usage.total（usage_settle 事件回填）
}

/** Phase 2c：Agent 级资源状态（RESTING 咖啡厅皮肤 + 环形倒计时数据源） */
interface AgentResource {
  state: string;               // ACTIVE / RESTING / SQUAD_DEPLOYED
  remainingSec: number | null; // 冷却剩余秒（tick 快照 refresh）
  restTotal: number;           // 本次 RESTING 的总时长（rate_limit_hit 时算出，环形比例分母）
  head: boolean;               // 部门 head 标识（GET /org_chart flat.head）
  department?: string | null;
}

/**
 * ★ Phase 3c：大盘站会视图（引擎事件 → 派生状态，全部"只装饰不布局"）
 * 数据源 = 后端事件流（§1.4 node_blocked / §1.5 tool_authorize / 纠错回路 rollback）：
 *   nodeBlocked   ← node_blocked（BFS 下游锁链；派生态：上游转绿/node_start 自动解锁）
 *   prRecords     ← pr_opened / pr_checks / pr_merged / pr_rejected（Git 实时流卡片）
 *   fixAttempts   ← rollback（顶栏"纠错进度 x/3"实心点数据源）
 *   authFlags     ← tool_authorize / auth_block（节点 ⚖ 需审批角标）
 *   redoNodes     ← rollback + node_start 派生（🚩 已带 QA 报错重做 小旗）
 *   gitCommits    ← git_commit（Git 实时流 终端流数据源）
 */
export interface BlockedInfo {
  reason: string;              // upstream_red（引擎权威）
  blockedBy: string[];         // 锁链路径（大盘"锁链描边"标签数据源）
  gate?: string;              // 触发拦截的门节点 kind（pr_gate/qa）
}
export interface PrRecord {
  prId: string;
  state: "open" | "merged" | "rejected";
  sourceBranches: string[];
  target?: string;
  backend?: string;
  checks: { name: string; passed: boolean; detail: string }[];
  redFlags: string[];
  mergeSha?: string;
  ts: number;
}
export interface FixAttempt {
  fromLevel: number;
  toLevel: number;
  attempt: number;            // 1..maxAttempts（拍板 #3 = 3）
  ts: number;
}
export interface AuthFlag {
  tool: string;
  sideEffect: string;          // none / local_write / external_side
  reason: string;
  allowed: boolean;
  approval?: string | null;   // 审批断点令牌（预留，§1.5）
}
export interface GitCommit {
  nodeId: string;
  branch: string;
  sha: string;
  msg: string;
  ts: number;
}

interface Store {
  workflow: Workflow | null;
  layouts: LevelLayout[];
  nodeIdToLevel: Record<string, number>;

  nodeStatus: Record<string, NodeStatus>;   // node_id → 状态
  nodeLoad: Record<string, NodeLoad>;       // ★ Phase 2c：node_id → 案卷负载
  agentRes: Record<string, AgentResource>;  // ★ Phase 2c：agent_id → 资源态/冷却/head
  runId: string | null;
  logEvents: BusEvent[];

  // ---- ★ Phase 3c：大盘派生状态（引擎事件驱动，前端只读消费）----
  nodeBlocked: Record<string, BlockedInfo>;  // node_id → 锁链（受阻琥珀描边）
  prRecords: Record<string, PrRecord>;       // pr_id → PR 卡片（Git 实时流）
  fixAttempts: FixAttempt[];                 // 纠错进度 x/3（顶栏徽标）
  authFlags: Record<string, AuthFlag>;       // node_id → 授权判定（⚖ 需审批角标）
  redoNodes: Record<string, string>;         // node_id → 带错重做说明（🚩 小旗）
  gitCommits: GitCommit[];                   // 提交证据（Git 实时流 终端流）
  approvalState: Record<string, "approved" | "rejected">;  // node_id → 人类审批决定（§1.5 断点记录）
  // ---- 3c 抽屉/弹窗 UI 态（点击交互）----
  expandedPr: string | null;                 // 抽屉里展开的 PR 卡片
  drawerFocus: string | null;                // 点击画布节点后抽屉浮出的焦点
  approvalTarget: string | null;             // ⚖ 角标点击 → 审批小窗

  /** 载入 workflow.json（fetch 本地或内联样本），一次算好全量坐标 */
  loadWorkflow(wf: Workflow): void;
  /** 处理一条 WS 事件：更新节点状态 + 追加时间线（去重上限 500 条） */
  onEvent(ev: BusEvent): void;
  /** 画布保存（kudosflow 式）：把当前节点图写回 workflow.json —— 这里只回写位置/标签，不碰 levels 结构 */
  markSaved(): void;
  lastSavedAt: number | null;
  /** ★ Phase 2c：部门 head 标识注入（GET /org_chart 后一次性写 agentRes.head） */
  markHeads(agents: Record<string, { head: boolean; department?: string | null }>): void;
  // ---- ★ Phase 3c 交互动作（点击装饰 → 抽屉/弹窗响应）----
  setExpandedPr(prId: string | null): void;
  setDrawerFocus(nodeId: string | null): void;
  setApprovalTarget(nodeId: string | null): void;
  setApproval(nodeId: string, decision: "approved" | "rejected"): void;
}

export const useWorkflowStore = create<Store>((set, get) => ({
  workflow: null,
  layouts: [],
  nodeIdToLevel: {},
  nodeStatus: {},
  nodeLoad: {},
  agentRes: {},
  runId: null,
  logEvents: [],
  lastSavedAt: null,
  nodeBlocked: {},
  prRecords: {},
  fixAttempts: [],
  authFlags: {},
  redoNodes: {},
  gitCommits: [],
  approvalState: {},
  expandedPr: null,
  drawerFocus: null,
  approvalTarget: null,

  loadWorkflow: (wf) => {
    const layouts = layoutAll(wf.levels);
    const nodeIdToLevel: Record<string, number> = {};
    for (const lv of wf.levels) for (const nd of lv.nodes) nodeIdToLevel[nd.id] = lv.index;
    set({ workflow: wf, layouts, nodeIdToLevel, nodeStatus: {}, nodeLoad: {}, agentRes: {}, logEvents: [] });
  },

  onEvent: (ev) => {
    const d = ev.data || {};
    set((s) => ({
      runId: ev.run_id || s.runId,
      logEvents: [...s.logEvents, ev].slice(-500),
    }));

    // ---- ★ Phase 3c：大盘派生状态（§1.4 锁链 / §1.5 授权 / 纠错回路 / Git 流）----
    if (ev.type === "run_start" && d.resuming_from == null) {
      // 全新 run（非回退重入）：run 维度派生态全部清零（BLOCKED 是派生位，新 run 重新算）
      set((s) => ({
        nodeBlocked: {}, prRecords: {}, fixAttempts: [], redoNodes: {},
        gitCommits: [], approvalState: {}, expandedPr: null, drawerFocus: null,
      }));
    }
    const nid = typeof d.node_id === "string" ? d.node_id : null;
    if (ev.type === "node_blocked" && nid) {
      // §1.4：pr_gate 红灯 → 该节点 + BFS 下游逐发 node_blocked（大盘琥珀锁链描边数据源）
      set((s) => ({
        nodeBlocked: { ...s.nodeBlocked, [nid]: {
          reason: String(d.reason ?? "upstream_red"),
          blockedBy: Array.isArray(d.blocked_by) ? (d.blocked_by as unknown[]).map(String) : [],
          gate: d.gate != null ? String(d.gate) : undefined,
        } },
      }));
    } else if (ev.type === "node_start" && nid) {
      // BLOCKED 派生不变量：上游转绿 → 节点重新调度 = 自动解锁（无需人工清，§1.4 ③）
      // + 纠错回路标记：回滚目标行上的 node_start（= Dev 被 qa_feedback 喂回重跑）→ 🚩 小旗
      set((s) => {
        const nb = { ...s.nodeBlocked };
        delete nb[nid];
        const out: Partial<Store> = { nodeBlocked: nb };
        const last = s.fixAttempts[s.fixAttempts.length - 1];
        if (last && s.nodeIdToLevel[nid] === last.toLevel) {
          out.redoNodes = { ...s.redoNodes, [nid]: `QA 红灯打回 · L${last.fromLevel}→L${last.toLevel} 第${last.attempt}次` };
        }
        return out;
      });
    } else if (ev.type === "pr_opened" && typeof d.pr === "string") {
      set((s) => ({
        prRecords: { ...s.prRecords, [String(d.pr)]: {
          prId: String(d.pr), state: "open",
          sourceBranches: Array.isArray(d.source_branches) ? (d.source_branches as unknown[]).map(String) : [],
          target: d.target != null ? String(d.target) : undefined,
          backend: d.backend != null ? String(d.backend) : undefined,
          checks: [], redFlags: [], ts: ev.ts,
        } },
      }));
    } else if ((ev.type === "pr_checks" || ev.type === "pr_merged" || ev.type === "pr_rejected") && typeof d.pr === "string") {
      set((s) => {
        const prId = String(d.pr);
        const rec = s.prRecords[prId] ?? { prId, state: "open" as const, sourceBranches: [], checks: [], redFlags: [], ts: ev.ts };
        const checks = Array.isArray(d.checks) ? (d.checks as { name: string; passed: boolean; detail: string }[]) : rec.checks;
        const redFlags = Array.isArray(d.red_flags) ? (d.red_flags as unknown[]).map(String) : rec.redFlags;
        const state: PrRecord["state"] =
          ev.type === "pr_merged" ? "merged" :
          ev.type === "pr_rejected" ? "rejected" :
          (d.state != null ? String(d.state) as PrRecord["state"] : rec.state);
        const out: Partial<Store> = {
          prRecords: { ...s.prRecords, [prId]: { ...rec, checks, redFlags, state,
            mergeSha: d.sha != null ? String(d.sha) : rec.mergeSha } },
        };
        // ★ 全绿收编 → 锁链自动解锁（§1.4：BLOCKED 派生态，上游转绿即消）
        if (ev.type === "pr_checks" && state === "merged" && nid) {
          const nb: Record<string, BlockedInfo> = {};
          for (const [k, v] of Object.entries(s.nodeBlocked)) if (!v.blockedBy.includes(nid)) nb[k] = v;
          out.nodeBlocked = nb;
        }
        return out;
      });
    } else if (ev.type === "rollback") {
      // 纠错回路：回滚事件 = 顶栏"纠错进度 x/3"实心点 + 🚩 小旗的触发源
      set((s) => ({
        fixAttempts: [...s.fixAttempts, {
          fromLevel: Number(d.from_level) || 0,
          toLevel: Number(d.to_level) || 0,
          attempt: Number(d.attempt) || s.fixAttempts.length + 1,
          ts: ev.ts,
        }],
      }));
    } else if (ev.type === "git_commit" && nid) {
      set((s) => ({
        gitCommits: [...s.gitCommits, {
          nodeId: nid, branch: String(d.branch ?? ""), sha: String(d.sha ?? ""),
          msg: String(d.msg ?? ""), ts: ev.ts,
        }].slice(-80),
      }));
    } else if ((ev.type === "tool_authorize" || ev.type === "auth_block") && nid) {
      // §1.5 中介授权层：external_side 未授权/高风险未审批 → 节点挂 ⚖ 需审批角标（可回放）
      set((s) => ({
        authFlags: { ...s.authFlags, [nid]: {
          tool: String(d.tool ?? ""), sideEffect: String(d.side_effect ?? ""),
          reason: String(d.reason ?? ""), allowed: d.allowed === true,
          approval: d.approval != null ? String(d.approval) : null,
        } },
      }));
    }

    // ---- ★ Phase 2c ①：工位案卷负载（load_change → 案卷高度数据源）----
    if (ev.type === "load_change") {
      const nid = d.node_id;
      if (typeof nid === "string") {
        set((s) => ({
          nodeLoad: { ...s.nodeLoad, [nid]: {
            estimate: Number(d.estimate_tok) || 0,
            source: d.source === "declared" ? "declared" : "estimated",
            actual: s.nodeLoad[nid]?.actual,
          } },
        }));
      }
    }
    // ---- ★ Phase 2c ②：真实 usage 回填（usage_settle 事件 → 案卷"实耗"角标）----
    if (ev.type === "usage_settle" && d.usage) {
      const nid = d.node_id;
      if (typeof nid === "string") {
        const total = Number((d.usage as { total_tokens?: number }).total_tokens) || 0;
        set((s) => {
          const cur = s.nodeLoad[nid] ?? { estimate: total, source: "estimated" as const };
          return { nodeLoad: { ...s.nodeLoad, [nid]: { ...cur, actual: total } } };
        });
      }
    }
    // ---- ★ Phase 2c ③：Agent 资源态（rate_limit_hit 定 RESTING 总量 / state_change / tick 刷新）----
    const agent = typeof d.agent === "string" ? d.agent : null;
    if (agent) {
      if (ev.type === "rate_limit_hit") {
        const now = Date.now() / 1000;
        const total = Math.max(1, Number(d.resume_at || 0) - now);
        set((s) => ({
          agentRes: { ...s.agentRes, [agent]: {
            ...(s.agentRes[agent] ?? { state: "ACTIVE", remainingSec: null, restTotal: 0, head: false }),
            state: "RESTING", remainingSec: total, restTotal: total,
          } },
        }));
      } else if (ev.type === "state_change" && d.state) {
        const now = Date.now() / 1000;
        set((s) => {
          const prev = s.agentRes[agent] ?? { state: "ACTIVE", remainingSec: null, restTotal: 0, head: false };
          if (d.state === "RESTING") {
            const total = Math.max(1, Number(d.resume_at || 0) - now);
            return { agentRes: { ...s.agentRes, [agent]: { ...prev, state: "RESTING", remainingSec: total, restTotal: total } } };
          }
          return { agentRes: { ...s.agentRes, [agent]: { ...prev, state: String(d.state), remainingSec: null, restTotal: 0 } } };
        });
      } else if (ev.type === "rest_over") {
        set((s) => s.agentRes[agent]
          ? { agentRes: { ...s.agentRes, [agent]: { ...s.agentRes[agent], state: "ACTIVE", remainingSec: null, restTotal: 0 } } }
          : {});
      }
    }
    if (ev.type === "tick" && d.states) {                       // tick 快照：全员资源态低频刷新（环形倒计时跟随）
      set((s) => {
        const nr: Record<string, AgentResource> = { ...s.agentRes };
        for (const [aid, st] of Object.entries(d.states as Record<string, { state: string; remaining_sec: number | null; department?: string | null }>)) {
          const prev = nr[aid] ?? { state: "ACTIVE", remainingSec: null, restTotal: 0, head: false };
          nr[aid] = {
            state: st.state,
            remainingSec: st.remaining_sec != null ? Math.max(0, st.remaining_sec) : null,
            restTotal: st.state === "RESTING" ? (prev.restTotal || st.remaining_sec || 0) : 0,
            head: prev.head,
            department: st.department ?? prev.department,
          };
        }
        return { agentRes: nr };
      });
    }

    // 节点状态机：WS 事件 → 画布节点变色（红框 = red_highlight / node_error）
    // nid 已由 3c 段计算（同函数作用域），此处直接复用
    if (!nid) return;
    const status = mapEventToStatus(ev.type);
    if (status) set((s) => ({ nodeStatus: { ...s.nodeStatus, [nid]: status } }));
  },

  markSaved: () => set({ lastSavedAt: Date.now() }),

  markHeads: (agents) => set((s) => {
    const nr: Record<string, AgentResource> = { ...s.agentRes };
    for (const [aid, info] of Object.entries(agents)) {
      const prev = nr[aid] ?? { state: "ACTIVE", remainingSec: null, restTotal: 0, head: false };
      nr[aid] = { ...prev, head: !!info.head, department: info.department ?? prev.department };
    }
    return { agentRes: nr };
  }),

  // ---- ★ Phase 3c 交互动作（点击装饰 → 抽屉/弹窗响应）----
  setExpandedPr: (prId) => set({ expandedPr: prId }),
  setDrawerFocus: (nodeId) => set({ drawerFocus: nodeId }),
  setApprovalTarget: (nodeId) => set({ approvalTarget: nodeId }),
  // §1.5 审批断点：人类决定落 approvalState（令牌签发/拒绝记录，全动作可回放）
  setApproval: (nodeId, decision) => set((s) => ({
    approvalState: { ...s.approvalState, [nodeId]: decision },
    approvalTarget: null,
  })),
}));

/** WS 事件 → 节点状态（ok 只在 run_finish 全局放行；节点级以 node_done/node_error 为准） */
function mapEventToStatus(type: string): NodeStatus | null {
  switch (type) {
    case "node_start": return "running";
    case "node_done": return "ok";
    case "node_error":
    case "red_highlight": return "error";
    default: return null;
  }
}

/** 从 workflow 构建 React Flow 节点/边（画布初始化） */
export function toReactFlow(wf: Workflow, layouts: ReturnType<typeof layoutAll>) {
  const pos: Record<string, { x: number; y: number }> = {};
  for (const lv of layouts) for (const n of lv.nodes) pos[n.nodeId] = n.position;

  // ★ Phase 2c：部门 head 集合（departments[] 里声明的 head → 前端「★ 部长」徽章）
  const heads = new Set<string>();
  const depts: unknown = (wf as { departments?: unknown }).departments;
  if (Array.isArray(depts)) {
    for (const d of depts as { head?: string }[]) if (d.head) heads.add(d.head);
  } else if (depts && typeof depts === "object") {
    for (const d of Object.values(depts as Record<string, { head?: string }>)) if (d.head) heads.add(d.head);
  }

  const nodes = wf.levels.flatMap((lv) =>
    lv.nodes.map((nd: NodeDef) => ({
      id: nd.id,
      type: "agentNode",                    // 注册 AgentNode 自定义节点
      position: pos[nd.id],
      data: {
        label: nd.label || wf.agents[nd.agent]?.role || nd.agent,
        agent: nd.agent,
        provider: wf.agents[nd.agent]?.provider ?? "?",
        tools: wf.agents[nd.agent]?.tools ?? [],
        kind: nd.kind ?? "agent",
        task: nd.task ?? "",
        // ★ Phase 2c 工位视觉数据源（静态兜底；实时值由 WS 事件经 store 覆盖）
        tokenBudget: nd.tokenBudget,
        head: heads.has(nd.agent),
        department: wf.agents[nd.agent]?.department ?? null,
      },
    }))
  );

  // 边：上游 inputs 引用（跨行数据流）+ 行序屏障（行尾 → 下一行首，仅视觉）
  const edges: { id: string; source: string; target: string; data?: Record<string, string> }[] = [];
  for (const lv of wf.levels) {
    for (const nd of lv.nodes) {
      for (const ref of nd.inputs ?? []) {
        const up = ref.split(".")[0];
        edges.push({ id: `${up}->${nd.id}`, source: up, target: nd.id, data: { role: "data" } });
      }
    }
  }
  for (let i = 0; i < wf.levels.length - 1; i++) {
    const cur = wf.levels[i].nodes, next = wf.levels[i + 1].nodes;
    edges.push({ id: `L${i}->L${i + 1}`, source: cur[cur.length - 1].id, target: next[0].id,
                 data: { role: "barrier" } });
  }
  return { nodes, edges };
}
