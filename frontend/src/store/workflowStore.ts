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

interface Store {
  workflow: Workflow | null;
  layouts: LevelLayout[];
  nodeIdToLevel: Record<string, number>;

  nodeStatus: Record<string, NodeStatus>;   // node_id → 状态
  runId: string | null;
  logEvents: BusEvent[];

  /** 载入 workflow.json（fetch 本地或内联样本），一次算好全量坐标 */
  loadWorkflow(wf: Workflow): void;
  /** 处理一条 WS 事件：更新节点状态 + 追加时间线（去重上限 500 条） */
  onEvent(ev: BusEvent): void;
  /** 画布保存（kudosflow 式）：把当前节点图写回 workflow.json —— 这里只回写位置/标签，不碰 levels 结构 */
  markSaved(): void;
  lastSavedAt: number | null;
}

export const useWorkflowStore = create<Store>((set, get) => ({
  workflow: null,
  layouts: [],
  nodeIdToLevel: {},
  nodeStatus: {},
  runId: null,
  logEvents: [],
  lastSavedAt: null,

  loadWorkflow: (wf) => {
    const layouts = layoutAll(wf.levels);
    const nodeIdToLevel: Record<string, number> = {};
    for (const lv of wf.levels) for (const nd of lv.nodes) nodeIdToLevel[nd.id] = lv.index;
    set({ workflow: wf, layouts, nodeIdToLevel, nodeStatus: {}, logEvents: [] });
  },

  onEvent: (ev) => {
    const d = ev.data || {};
    set((s) => ({
      runId: ev.run_id || s.runId,
      logEvents: [...s.logEvents, ev].slice(-500),
    }));
    // 节点状态机：WS 事件 → 画布节点变色（红框 = red_highlight / node_error）
    const nid = typeof d.node_id === "string" ? d.node_id : null;
    if (!nid) return;
    const status = mapEventToStatus(ev.type);
    if (status) set((s) => ({ nodeStatus: { ...s.nodeStatus, [nid]: status } }));
  },

  markSaved: () => set({ lastSavedAt: Date.now() }),
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
