/** workflow_schema.json (draft-07) 的 TypeScript 镜像 —— 前后端共用同一份契约词汇。 */

export type NodeKind = "agent" | "qa" | "review" | "gate" | "squad" | "pr_gate";
export type ProviderId = "ollama" | "codex" | "claude" | "mock" | "builtin";

export interface AgentDef {
  id: string;
  role?: string;
  provider: ProviderId | string;
  model?: string;
  tools?: string[];
  systemPrompt?: string;
  maxTurns?: number;
  outputs?: { name: string; kind?: string; path?: string }[];
  department?: string;              // ★ Phase 2b/2c：部门归属（head 标识数据源）
}

export interface NodeDef {
  id: string;
  label?: string;
  agent: string;
  kind?: NodeKind;
  task?: string;
  inputs?: string[];          // "upstreamNodeId.outputName"
  reviewCriteria?: string;
  onError?: { policy?: "failFast" | "retry" | "skip"; rollbackToLevel?: number };
  tokenBudget?: { estimate?: number; contextHint?: number; outputHint?: number };  // ★ Phase 2c：工位案卷高度数据源
}

export interface LevelDef {
  index: number;             // ★ 行号：渲染 Y 坐标的唯一来源（同级同行不变量）
  name: string;
  gate?: "all" | "any";
  nodes: NodeDef[];
}

export interface QaPolicy {
  onFailure?: string[];
  rollback?: { targetLevel?: number; keepCheckpoint?: boolean };
  retry?: { maxAttempts?: number; backoffSeconds?: number };
}

export interface Workflow {
  meta?: { name?: string; version?: string; projectRoot?: string; timezone?: string };
  agents: Record<string, AgentDef>;
  levels: LevelDef[];
  qa?: QaPolicy;
}

/** 引擎事件（ws/stream.py 广播）—— 前端 LiveLogPanel 与节点状态机消费。 */
export type BusEvent =
  | { ts: number; run_id: string; type: string; data: Record<string, unknown> };

export type NodeStatus = "idle" | "running" | "ok" | "error";
