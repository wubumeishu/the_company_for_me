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

interface Store {
  workflow: Workflow | null;
  layouts: LevelLayout[];
  nodeIdToLevel: Record<string, number>;

  nodeStatus: Record<string, NodeStatus>;   // node_id → 状态
  nodeLoad: Record<string, NodeLoad>;       // ★ Phase 2c：node_id → 案卷负载
  agentRes: Record<string, AgentResource>;  // ★ Phase 2c：agent_id → 资源态/冷却/head
  runId: string | null;
  logEvents: BusEvent[];

  /** 载入 workflow.json（fetch 本地或内联样本），一次算好全量坐标 */
  loadWorkflow(wf: Workflow): void;
  /** 处理一条 WS 事件：更新节点状态 + 追加时间线（去重上限 500 条） */
  onEvent(ev: BusEvent): void;
  /** 画布保存（kudosflow 式）：把当前节点图写回 workflow.json —— 这里只回写位置/标签，不碰 levels 结构 */
  markSaved(): void;
  lastSavedAt: number | null;
  /** ★ Phase 2c：部门 head 标识注入（GET /org_chart 后一次性写 agentRes.head） */
  markHeads(agents: Record<string, { head: boolean; department?: string | null }>): void;
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
    const nid = typeof d.node_id === "string" ? d.node_id : null;
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
